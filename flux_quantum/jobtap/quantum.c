/*
 * Site policy for quantum and classical coscheduling.
 *
 * The submit plugin stamps the held classical half of a pair with
 * attributes.system.quantum.vendor and attributes.system.quantum.cores.
 * The scout carries neither, only its qpu request.
 *
 * Vendors. Only configured vendors are permitted.
 *
 * Protection. Both halves of a pair get attributes.system.protected, which
 * exempts them from preemption. The plugin sets it and a submission that
 * carries it is rejected.
 *
 * Admission. A scout costs money once it holds a vendor session, so a pair
 * is admitted only if its cores plus one for the scout fit in what is
 * reachable. Reachable means cores nothing is running on, plus cores held by
 * unprotected work that could be preempted, less cores promised to pairs
 * that were admitted but have not started. The job table behind this is
 * built in job.new, which the job manager replays on restart and reload.
 *
 * Preemption. When the classical half reaches SCHED a timer starts, and if
 * it has not started by preempt_after seconds the youngest unprotected jobs
 * are cancelled until enough cores are free. The clock starts at submission
 * rather than at the scout's release, because on a full machine the scout
 * cannot get a core and there would be no release to wait for. A vendor
 * queue longer than the timer therefore frees cores before the session
 * exists. Cancelling is destructive, so preemption is off unless
 * preempt_after is set.
 *
 * Load with
 *   flux jobtap load quantum.so vendors=ibm,braket total_cores=128 \
 *       reserve_cores=32 protect_types=qpu preempt_after=30
 *
 * total_cores 0 disables admission control. protect_types names the resource
 * types that mark a job as one of ours, default qpu. flux jobtap query
 * reports the configuration in force and the capacity numbers.
 */
#include <string.h>
#include <stdlib.h>
#include <stdint.h>
#include <errno.h>
#include <syslog.h>
#include <jansson.h>
#include <flux/core.h>
#include <flux/jobtap.h>

static char *g_vendors = NULL;         /* comma separated, default ibm,braket */
static char *g_protect_types = NULL;   /* comma separated, default qpu */
static double g_preempt_after = 0.0;   /* 0 disables preemption */
static int g_total_cores = 0;          /* 0 disables admission control */
static int g_reserve_cores = 0;

/* every job the plugin has seen, keyed by jobid, with its cores, whether it
 * is protected, whether it is the classical half of a pair, and the time it
 * started or 0 */
static json_t *g_jobs = NULL;

/* Not under quantum on purpose, so a plugin protecting some other scarce
 * resource can use the same key. The value is a reason and only presence
 * matters. */
static const char *PROTECT_KEY = "attributes.system.protected";

static int type_protected (const char *type)
{
    const char *list = g_protect_types ? g_protect_types : "qpu";
    size_t tlen = strlen (type);
    const char *p = list;
    while (*p) {
        const char *comma = strchr (p, ',');
        size_t seg = comma ? (size_t)(comma - p) : strlen (p);
        if (seg == tlen && strncmp (p, type, tlen) == 0)
            return 1;
        if (!comma)
            break;
        p = comma + 1;
    }
    return 0;
}

/* walk a v1 resource tree looking for a protected type */
static int tree_wants_protection (json_t *resources)
{
    size_t i;
    json_t *entry;

    if (!json_is_array (resources))
        return 0;
    json_array_foreach (resources, i, entry) {
        const char *type = NULL;
        json_t *with = NULL;
        if (json_unpack (entry, "{s:s}", "type", &type) == 0
            && type
            && type_protected (type))
            return 1;
        if (json_unpack (entry, "{s:o}", "with", &with) == 0
            && tree_wants_protection (with))
            return 1;
    }
    return 0;
}

/* True when the job is one half of a pair. The classical carries the vendor
 * attribute, the scout carries the resource request. */
static int needs_protection (json_t *jobspec, const char *vendor)
{
    json_t *resources = NULL;

    if (vendor)
        return 1;
    if (json_unpack (jobspec, "{s:o}", "resources", &resources) == 0)
        return tree_wants_protection (resources);
    return 0;
}

/* A user who sets the key would make their own job unpreemptible, so the job
 * is rejected rather than quietly stripped. */
static int user_set_protection (json_t *jobspec)
{
    json_t *val = NULL;
    (void) json_unpack (jobspec, "{s:{s:{s:o}}}",
                        "attributes", "system", "protected", &val);
    return val ? 1 : 0;
}

static const char *idkey (flux_jobid_t id, char *buf, size_t len)
{
    snprintf (buf, len, "%ju", (uintmax_t) id);
    return buf;
}

static void track (flux_jobid_t id,
                   int cores,
                   int protected_,
                   int pair,
                   double run)
{
    char key[32];
    json_t *entry;

    if (!g_jobs)
        return;
    entry = json_pack ("{s:i s:b s:b s:f}",
                       "cores", cores, "protected", protected_,
                       "pair", pair, "run", run);
    if (entry)
        (void) json_object_set_new (g_jobs, idkey (id, key, sizeof (key)), entry);
}

static void forget (flux_jobid_t id)
{
    char key[32];
    if (g_jobs)
        (void) json_object_del (g_jobs, idkey (id, key, sizeof (key)));
}

static json_t *lookup (flux_jobid_t id)
{
    char key[32];
    if (!g_jobs)
        return NULL;
    return json_object_get (g_jobs, idkey (id, key, sizeof (key)));
}

static int budget (void)
{
    int b = g_total_cores - g_reserve_cores;
    return b > 0 ? b : 0;
}

/* Free is the budget less everything running. Preemptible is what running
 * unprotected work holds. Promised is what admitted pairs that have not
 * started will need, which still looks free and must not be handed out
 * twice. A running pair is in neither of the last two. */
static void capacity (int *freep, int *preemptiblep, int *promisedp)
{
    const char *key;
    json_t *entry;
    int running = 0, preemptible = 0, promised = 0;

    if (g_jobs) {
        json_object_foreach (g_jobs, key, entry) {
            int cores = 0, prot = 0, pair = 0;
            double run = 0.0;

            if (json_unpack (entry,
                             "{s:i s:b s:b s:F}",
                             "cores", &cores,
                             "protected", &prot,
                             "pair", &pair,
                             "run", &run) < 0)
                continue;
            if (cores <= 0)
                continue;
            if (run > 0.0) {
                running += cores;
                if (!prot)
                    preemptible += cores;
            } else if (pair) {
                promised += cores;
            }
        }
    }
    *freep = budget () - running > 0 ? budget () - running : 0;
    *preemptiblep = preemptible;
    *promisedp = promised;
}

/* the classical's cores plus one for the scout */
static int pair_cores (json_t *jobspec)
{
    json_int_t cores = 0;
    if (json_unpack (jobspec, "{s:{s:{s:{s:I}}}}",
                     "attributes", "system", "quantum", "cores", &cores) < 0)
        return 0;
    if (cores < 0)
        return 0;
    return (int)cores + 1;
}

/* cores any job asks for, from its resource tree */
static int count_tree_cores (json_t *resources, int factor);

static int count_tree_cores_top (json_t *jobspec)
{
    json_t *resources = NULL;
    if (json_unpack (jobspec, "{s:o}", "resources", &resources) < 0)
        return 0;
    return count_tree_cores (resources, 1);
}

static int count_tree_cores (json_t *resources, int factor)
{
    size_t i;
    json_t *entry;
    int total = 0;

    if (!json_is_array (resources))
        return 0;
    json_array_foreach (resources, i, entry) {
        const char *type = NULL;
        json_t *with = NULL;
        json_int_t count = 1;
        int n;

        (void) json_unpack (entry, "{s?I}", "count", &count);
        if (count < 1)
            count = 1;
        n = factor * (int) count;
        if (json_unpack (entry, "{s:s}", "type", &type) == 0
            && type
            && strcmp (type, "core") == 0)
            total += n;
        if (json_unpack (entry, "{s:o}", "with", &with) == 0)
            total += count_tree_cores (with, n);
    }
    return total;
}

/* the vendor attribute is what makes a job one of ours */
static const char *job_vendor (json_t *jobspec)
{
    const char *vendor = NULL;
    (void) json_unpack (jobspec, "{s:{s:{s:{s:s}}}}",
                        "attributes", "system", "quantum", "vendor", &vendor);
    return vendor;
}

static int vendor_allowed (const char *vendor)
{
    const char *list = g_vendors ? g_vendors : "ibm,braket";
    size_t vlen = strlen (vendor);
    const char *p = list;
    while (*p) {
        const char *comma = strchr (p, ',');
        size_t seg = comma ? (size_t)(comma - p) : strlen (p);
        if (seg == vlen && strncmp (p, vendor, vlen) == 0)
            return 1;
        if (!comma)
            break;
        p = comma + 1;
    }
    return 0;
}

static int validate_cb (flux_plugin_t *p,
                        const char *topic,
                        flux_plugin_arg_t *args,
                        void *arg)
{
    json_t *jobspec = NULL;
    const char *vendor = NULL;

    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN,
                                "{s:o}", "jobspec", &jobspec) < 0)
        return 0;   /* can't read jobspec; do not block the job */

    if (user_set_protection (jobspec))
        return flux_jobtap_reject_job (p, args,
                   "attributes.system.protected is set by the scheduler and "
                   "must not be set on a submission. Remove it. It marks a job "
                   "as not preemptible, which is not yours to decide");

    /* attributes.system.quantum.vendor -- absent => not a quantum job */
    vendor = job_vendor (jobspec);

    if (vendor && !vendor_allowed (vendor))
        return flux_jobtap_reject_job (p, args,
                   "quantum: vendor '%s' is not permitted at this site", vendor);

    if (vendor && g_total_cores > 0) {
        int want = pair_cores (jobspec);
        if (want <= 0)
            return flux_jobtap_reject_job (p, args,
                       "quantum: attributes.system.quantum.cores is missing, "
                       "so this pair cannot be accounted for");
        int freec = 0, preemptible = 0, promised = 0;
        capacity (&freec, &preemptible, &promised);
        if (want + promised > freec + preemptible)
            return flux_jobtap_reject_job (p, args,
                       "quantum: no room for another pair. This one needs %d "
                       "cores, %d are already promised to pairs that have not "
                       "started, and only %d are reachable (%d free, %d that "
                       "could be preempted). Wait for one to finish or ask for "
                       "fewer cores",
                       want, promised, freec + preemptible, freec, preemptible);
    }

    /* Mark it protected last. A jobspec update left pending on a rejected
     * job makes job.destroy complain. The scout is protected too, or
     * preempting it would leak a vendor session. */
    if (needs_protection (jobspec, vendor)) {
        if (flux_jobtap_jobspec_update_pack (p, "{s:s}",
                                             PROTECT_KEY, "quantum") < 0)
            flux_log (flux_jobtap_get_flux (p), LOG_ERR,
                      "quantum: could not mark the job protected, it would be "
                      "preemptible");
    }
    return 0;
}

/* Cancel unprotected running jobs, youngest first, until need cores are freed.
 * Returns the cores freed, which may be less than asked for if there is not
 * enough unprotected work to take. */
static int preempt_for (flux_plugin_t *p, flux_jobid_t sparing, int need)
{
    flux_t *h = flux_jobtap_get_flux (p);
    int freed = 0;

    while (freed < need) {
        const char *key, *chosen = NULL;
        json_t *entry;
        double newest = -1.0;
        int cores = 0;

        json_object_foreach (g_jobs, key, entry) {
            int prot = 0, c = 0;
            double run = 0.0;
            char spare[32];

            if (json_unpack (entry, "{s:i s:b s:F}",
                             "cores", &c, "protected", &prot, "run", &run) < 0)
                continue;
            if (prot || run <= 0.0 || c <= 0)
                continue;   /* protected, or not running, so not a candidate */
            if (strcmp (key, idkey (sparing, spare, sizeof (spare))) == 0)
                continue;
            if (run > newest) {
                newest = run;
                chosen = key;
                cores = c;
            }
        }
        if (!chosen)
            break;      /* nothing left to take */

        if (flux_jobtap_raise_exception (p,
                                         (flux_jobid_t) strtoull (chosen, NULL, 10),
                                         "preempt",
                                         0,
                                         "preempted to free %d cores for a "
                                         "quantum job whose session is already "
                                         "open", need) < 0) {
            flux_log_error (h, "quantum: could not preempt job %s", chosen);
            break;
        }
        flux_log (h, LOG_WARNING,
                  "quantum: preempted job %s for %d cores", chosen, cores);
        freed += cores;
        /* it is going away, so do not pick it again on the next pass */
        (void) json_object_del (g_jobs, chosen);
    }
    return freed;
}

/* Pending grace timers are kept on a list so unloading the plugin can destroy
 * them. One firing after unload would take the job manager down. */
struct grace {
    flux_plugin_t *p;
    flux_jobid_t id;
    int cores;
    flux_watcher_t *w;
    struct grace *next;
};

static struct grace *g_grace = NULL;

static void grace_destroy (struct grace *g)
{
    struct grace **pp;

    for (pp = &g_grace; *pp; pp = &(*pp)->next) {
        if (*pp == g) {
            *pp = g->next;
            break;
        }
    }
    flux_watcher_destroy (g->w);
    free (g);
}

static void grace_cb (flux_reactor_t *r,
                      flux_watcher_t *w,
                      int revents,
                      void *arg)
{
    struct grace *g = arg;
    json_t *entry = lookup (g->id);
    double run = 0.0;

    /* started on its own, so there is nothing to do */
    if (entry
        && json_unpack (entry, "{s:F}", "run", &run) == 0
        && run > 0.0)
        goto done;
    if (!entry)
        goto done;      /* finished or vanished */

    flux_log (flux_jobtap_get_flux (g->p), LOG_WARNING,
              "quantum: job %ju still not started after %.0fs, preempting for "
              "%d cores", (uintmax_t) g->id, g_preempt_after, g->cores);
    (void) preempt_for (g->p, g->id, g->cores);
done:
    grace_destroy (g);
}

/* start the grace timer when the classical half of a pair reaches SCHED */
static int sched_cb (flux_plugin_t *p,
                     const char *topic,
                     flux_plugin_arg_t *args,
                     void *arg)
{
    flux_jobid_t id;
    json_t *entry;
    struct grace *g;
    int cores = 0, pair = 0;
    flux_t *h;

    if (g_preempt_after <= 0.0)
        return 0;
    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN, "{s:I}", "id", &id) < 0)
        return 0;
    if (!(entry = lookup (id))
        || json_unpack (entry, "{s:i s:b}", "cores", &cores, "pair", &pair) < 0
        || !pair
        || cores <= 0)
        return 0;   /* not the classical half of a pair */

    h = flux_jobtap_get_flux (p);
    if (!(g = calloc (1, sizeof (*g))))
        return 0;
    g->p = p;
    g->id = id;
    g->cores = cores + 1;   /* neither half may be running, make room for both */
    if (!(g->w = flux_timer_watcher_create (flux_get_reactor (h),
                                            g_preempt_after, 0.,
                                            grace_cb, g))) {
        free (g);
        return 0;
    }
    g->next = g_grace;
    g_grace = g;
    flux_watcher_start (g->w);
    return 0;
}

/* called for new jobs, and replayed for active ones on restart and reload */
static int new_cb (flux_plugin_t *p,
                   const char *topic,
                   flux_plugin_arg_t *args,
                   void *arg)
{
    json_t *jobspec = NULL;
    flux_jobid_t id;
    const char *vendor;
    int state = 0;
    double t_submit = 0.0;
    double run = 0.0;

    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN,
                                "{s:I s:o s?i s?F}",
                                "id", &id,
                                "jobspec", &jobspec,
                                "state", &state,
                                "t_submit", &t_submit) < 0)
        return 0;
    vendor = job_vendor (jobspec);

    /* job.state.run is not replayed, so a job already running on replay
     * would otherwise look idle. The start time is not in the args either, so
     * the submit time stands in and keeps the youngest first order. */
    if (state == FLUX_JOB_STATE_RUN || state == FLUX_JOB_STATE_CLEANUP)
        run = t_submit > 0.0 ? t_submit : 1.0;

    /* Every job is tracked, since any unprotected one is a preemption
     * candidate. Each entry holds what that job alone asks for. The scout has
     * its own entry, so the classical is not charged for it. */
    track (id,
           count_tree_cores_top (jobspec),
           needs_protection (jobspec, vendor),
           vendor ? 1 : 0,
           run);
    return 0;
}

static int destroy_cb (flux_plugin_t *p,
                       const char *topic,
                       flux_plugin_arg_t *args,
                       void *arg)
{
    flux_jobid_t id;

    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN,
                                "{s:I}", "id", &id) < 0)
        return 0;
    forget (id);
    return 0;
}

static int run_cb (flux_plugin_t *p,
                   const char *topic,
                   flux_plugin_arg_t *args,
                   void *arg)
{
    flux_jobid_t id;
    json_t *entry;

    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN,
                                "{s:I}", "id", &id) < 0)
        return 0;
    if ((entry = lookup (id)))
        (void) json_object_set_new (entry, "run",
                                    json_real (flux_reactor_now (
                                        flux_get_reactor (
                                            flux_jobtap_get_flux (p)))));
    return 0;
}

/* flux jobtap query. Reports the configuration in force, so an experiment can
 * record what it actually ran with, and the current capacity numbers. */
static int query_cb (flux_plugin_t *p,
                     const char *topic,
                     flux_plugin_arg_t *args,
                     void *arg)
{
    int freec = 0, preemptible = 0, promised = 0;

    capacity (&freec, &preemptible, &promised);
    if (flux_plugin_arg_pack (args,
                              FLUX_PLUGIN_ARG_OUT,
                              "{s:i s:i s:f s:s s:s s:i s:i s:i s:i}",
                              "total_cores", g_total_cores,
                              "reserve_cores", g_reserve_cores,
                              "preempt_after", g_preempt_after,
                              "vendors", g_vendors ? g_vendors : "",
                              "protect_types", g_protect_types ? g_protect_types : "",
                              "free_cores", freec,
                              "preemptible_cores", preemptible,
                              "promised_cores", promised,
                              "tracked_jobs", g_jobs ? (int) json_object_size (g_jobs) : 0)
        < 0)
        return -1;
    return 0;
}

static void plugin_destroy (void *arg)
{
    while (g_grace)
        grace_destroy (g_grace);
    json_decref (g_jobs);
    g_jobs = NULL;
    free (g_vendors);
    g_vendors = NULL;
    free (g_protect_types);
    g_protect_types = NULL;
}

static const struct flux_plugin_handler handlers[] = {
    { "plugin.query",     query_cb,    NULL },
    { "job.validate",     validate_cb, NULL },
    { "job.new",          new_cb,      NULL },
    { "job.state.run",    run_cb,      NULL },
    { "job.state.sched",  sched_cb,    NULL },
    { "job.destroy",      destroy_cb,  NULL },
    { 0 },
};

int flux_plugin_init (flux_plugin_t *p)
{
    const char *vendors = NULL;
    const char *protect_types = NULL;
    int total = 0, reserve = 0;
    double preempt_after = 0.0;
    flux_t *h = flux_jobtap_get_flux (p);
    int rc;

    if (!(g_jobs = json_object ()))
        return -1;
    if (flux_plugin_aux_set (p, NULL, g_jobs, plugin_destroy) < 0) {
        json_decref (g_jobs);
        g_jobs = NULL;
        return -1;
    }

    /* A config that cannot be read would leave admission control and
     * preemption off without saying so, so refuse to load. ENOENT just means
     * no config was given. */
    rc = flux_plugin_conf_unpack (p, "{s?s s?s s?i s?i s?F}",
                                  "vendors", &vendors,
                                  "protect_types", &protect_types,
                                  "total_cores", &total,
                                  "reserve_cores", &reserve,
                                  "preempt_after", &preempt_after);
    if (rc < 0 && errno != ENOENT) {
        flux_log (h, LOG_ERR,
                  "quantum: cannot read plugin config: %s. Not loading, "
                  "because loading with defaults would leave admission control "
                  "and preemption off without saying so",
                  flux_plugin_strerror (p));
        return -1;
    }
    if (rc == 0) {
        if (vendors)
            g_vendors = strdup (vendors);
        if (protect_types)
            g_protect_types = strdup (protect_types);
        g_total_cores = total;
        g_reserve_cores = reserve;
        g_preempt_after = preempt_after;
    }

    flux_log (h, LOG_INFO,
              "quantum: vendors=%s total_cores=%d reserve_cores=%d "
              "protect_types=%s preempt_after=%.1f",
              g_vendors ? g_vendors : "(default)",
              g_total_cores,
              g_reserve_cores,
              g_protect_types ? g_protect_types : "(default)",
              g_preempt_after);
    if (g_total_cores <= 0)
        flux_log (h, LOG_WARNING,
                  "quantum: total_cores is not set, so admission control is "
                  "off and a pair may be admitted that cannot be placed");
    if (g_preempt_after > 0.0)
        flux_log (h, LOG_WARNING,
                  "quantum: preemption is on. An unprotected job may be "
                  "cancelled to make room for a pair whose session is "
                  "already open, and a cancelled job loses its work");
    else
        flux_log (h, LOG_WARNING,
                  "quantum: preemption is off. A pair that cannot be placed "
                  "will hold an open vendor session until the machine frees "
                  "up on its own");

    if (flux_plugin_register (p, "quantum", handlers) < 0) {
        flux_log_error (h, "quantum: could not register handlers");
        return -1;
    }

    return 0;
}

/* vi: ts=4 sw=4 expandtab
 */
