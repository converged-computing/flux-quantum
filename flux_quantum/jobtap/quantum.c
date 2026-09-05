/*
 * quantum-jobtap: site policy for quantum and classical coscheduling.
 *
 * A quantum job is the held classical half of a pair, identified by
 *   attributes.system.quantum.vendor = "<vendor>"
 *   attributes.system.quantum.cores  = <cores the classical asks for>
 * both stamped by the submit plugin. The scout carries neither, so it is not
 * counted twice.
 *
 * Three jobs enforced here.
 *
 * Vendors. Only configured vendors are permitted.
 *
 * Protection. Both halves of a pair are marked
 *   attributes.system.protected = "quantum"
 * which exempts them from preemption. The key is generic on purpose, so a
 * plugin protecting any other scarce external resource can reuse it. It is set
 * here and never by the submitter, and a submission carrying it is rejected.
 *
 * Admission. Launching a scout commits real money the moment it acquires a
 * vendor session, so the classical half has to be placeable. Every unfinished
 * pair, held or running, reserves cores + 1 against a budget, and a new pair
 * that would exceed the budget is rejected at submit rather than admitted and
 * left to wait with a meter running. Held pairs count because a held pair is a
 * promise.
 *
 * Accounting lives in job.new and not job.validate, because the job manager
 * replays job.new for every active job on restart or plugin reload, so the
 * budget rebuilds itself. job.destroy gives the cores back.
 *
 * Load:
 *   flux jobtap load quantum.so vendors="ibm,braket" \
 *        total_cores=128 reserve_cores=32
 *
 *   total_cores    cores available to quantum pairs. 0 disables admission
 *                  control and leaves only the vendor check.
 *   reserve_cores  held back for ordinary classical work.
 *   protect_types  resource types that mark a job as needing external access,
 *                  default qpu. Used to protect the scout, which carries no
 *                  quantum attributes.
 *   preempt_after  seconds to wait after the scout releases the classical
 *                  before cancelling unprotected jobs to make room. 0, the
 *                  default, never preempts and the pair just waits.
 *
 * Preemption. Admission promises that the classical half can be placed, but
 * ordinary work may be sitting on the cores when the scout releases it. So the
 * plugin watches for the session memo, which the scout posts immediately before
 * releasing, waits preempt_after seconds, and if the job still has not started
 * it cancels unprotected jobs until enough cores are freed. Youngest first,
 * because flux cancels rather than requeues and the youngest job loses the
 * least work.
 *
 * NOTES
 *   Cancelling is destructive. The victim loses whatever it had done, so
 *   preemption is off unless preempt_after is set. Protected jobs are never
 *   victims, and both halves of a pair are protected, so preemption cannot eat
 *   another pair.
 *
 *   Freeing cores is not instant. Cancel, then epilog, then the scheduler
 *   notices. The classical job starts after that, not at the moment of the
 *   cancel, and the vendor session covers the gap.
 */
#include <string.h>
#include <stdlib.h>
#include <stdint.h>
#include <syslog.h>
#include <jansson.h>
#include <flux/core.h>
#include <flux/jobtap.h>

/* default vendor allowlist; over/replace via load-time config "vendors=" */
static char *g_vendors = NULL;   /* comma-separated, e.g. "ibm,braket" */

/* Jobs the plugin knows about, keyed by jobid as a string.
 *   id -> cores, protected, run
 * A running unprotected job is a preemption candidate, and the run time orders
 * them so the youngest goes first.
 */
static json_t *g_jobs = NULL;

static double g_preempt_after = 0.0;   /* 0 disables preemption */

static int g_total_cores = 0;    /* 0 means admission control is off */
static int g_reserve_cores = 0;
static int g_used_cores = 0;     /* cores promised to unfinished pairs */

/* marks a job as counted, so job.destroy only gives back what was taken */
static const char *AUX_KEY = "quantum::cores";

/* Marks a job as not preemptible. Deliberately not under quantum, so any
 * policy plugin protecting a scarce external resource can use the same key and
 * the preemption logic stays generic. The value is a reason, and preemption
 * cares only that it is present. */
static const char *PROTECT_KEY = "attributes.system.protected";

/* resource types that identify a job as needing external quantum access. The
 * scout carries no quantum attributes, only this request, and a request cannot
 * be faked into existence because fluxion has to match it against the graph. */
static char *g_protect_types = NULL;   /* comma separated, e.g. "qpu" */

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

/* Set by this plugin, never by the submitter. A user who sets it would make
 * their own job unpreemptible, so it is rejected rather than stripped, because
 * silently ignoring an attempted privilege grab hides it. */
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

/* remember a job so it can be considered as a victim later */
static void track (flux_jobid_t id, int cores, int protected_, int pair)
{
    char key[32];
    json_t *entry;

    if (!g_jobs)
        return;
    entry = json_pack ("{s:i s:b s:b s:f}",
                       "cores", cores, "protected", protected_,
                       "pair", pair, "run", 0.0);
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

/* cores this pair reserves. The scout needs one of its own, so a classical of
 * n cores makes the pair n + 1. */
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

/* cores an ordinary job asks for. The submit plugin stamps the count for our
 * own pairs, but a victim candidate is any job, so count its tree. */
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
    (void) json_unpack (jobspec, "{s:{s:{s:{s:s}}}}",
                        "attributes", "system", "quantum", "vendor", &vendor);

    /* the scout has no quantum attributes, only the resource request, and it
     * must be protected too or preempting it would leak a vendor session */
    if (needs_protection (jobspec, vendor)) {
        if (flux_jobtap_jobspec_update_pack (p, "{s:s}",
                                             PROTECT_KEY, "quantum") < 0)
            flux_log (flux_jobtap_get_flux (p), LOG_ERR,
                      "quantum: could not mark the job protected, it would be "
                      "preemptible");
    }

    if (!vendor)
        return 0;

    if (!vendor_allowed (vendor))
        return flux_jobtap_reject_job (p, args,
                   "quantum: vendor '%s' is not permitted at this site", vendor);

    if (g_total_cores > 0) {
        int want = pair_cores (jobspec);
        if (want <= 0)
            return flux_jobtap_reject_job (p, args,
                       "quantum: attributes.system.quantum.cores is missing, "
                       "so this pair cannot be accounted for");
        if (g_used_cores + want > budget ())
            return flux_jobtap_reject_job (p, args,
                       "quantum: no room for another pair. %d of %d cores are "
                       "promised to unfinished pairs and this one needs %d. "
                       "Wait for one to finish or ask for fewer cores",
                       g_used_cores, budget (), want);
    }

    /* policy satisfied; the job proceeds. The hold is set at submission by the
     * submit plugin, and the scout releases it once the session is live. */
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

struct grace {
    flux_plugin_t *p;
    flux_jobid_t id;
    int cores;
    flux_watcher_t *w;
};

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
    flux_watcher_destroy (w);
    free (g);
}

/* Start the clock when the classical half reaches SCHED.
 *
 * The memo would be the natural signal, since the scout posts it immediately
 * before releasing. But on a full machine the scout cannot get a core either,
 * so there is no memo and the pair waits forever with nothing to trigger on.
 * Admission already promised this pair room, so the promise is what the clock
 * hangs off, not the handover.
 */
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
    /* cores is the whole pair, classical plus one for the scout. Neither may
     * be running, so make room for both. */
    g->cores = cores;
    if (!(g->w = flux_timer_watcher_create (flux_get_reactor (h),
                                            g_preempt_after, 0.,
                                            grace_cb, g))) {
        free (g);
        return 0;
    }
    flux_watcher_start (g->w);
    return 0;
}

/* Accounting. Called for new jobs and replayed for active ones on restart or
 * plugin reload, which is how the budget survives both. */
static int new_cb (flux_plugin_t *p,
                   const char *topic,
                   flux_plugin_arg_t *args,
                   void *arg)
{
    json_t *jobspec = NULL;
    flux_jobid_t id;
    const char *vendor;
    int want;

    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN,
                                "{s:I s:o}", "id", &id,
                                "jobspec", &jobspec) < 0)
        return 0;
    vendor = job_vendor (jobspec);

    /* Track every job, not only ours. An unprotected running job is what a
     * quantum pair preempts, so the plugin has to know they exist. Called
     * again on replay, and json_object_set overwrites, so this is idempotent
     * apart from losing the run time, which the state callback restores. */
    track (id,
           vendor ? pair_cores (jobspec) : count_tree_cores_top (jobspec),
           needs_protection (jobspec, vendor),
           vendor ? 1 : 0);
    if (g_preempt_after > 0.0)
        (void) flux_jobtap_job_subscribe (p, id);

    if (g_total_cores <= 0)
        return 0;
    if (!vendor)
        return 0;
    if ((want = pair_cores (jobspec)) <= 0)
        return 0;

    /* record what was taken, so destroy gives back the same amount even if the
     * jobspec is no longer readable by then */
    if (flux_jobtap_job_aux_set (p, id, AUX_KEY,
                                 (void *)(intptr_t)want, NULL) < 0)
        return 0;
    g_used_cores += want;
    return 0;
}

static int destroy_cb (flux_plugin_t *p,
                       const char *topic,
                       flux_plugin_arg_t *args,
                       void *arg)
{
    flux_jobid_t id;
    intptr_t want;

    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN,
                                "{s:I}", "id", &id) < 0)
        return 0;
    /* a job rejected at validate was never counted, so there is no aux and
     * nothing to give back */
    forget (id);
    want = (intptr_t) flux_jobtap_job_aux_get (p, id, AUX_KEY);
    if (want <= 0)
        return 0;
    g_used_cores -= (int)want;
    if (g_used_cores < 0)
        g_used_cores = 0;
    return 0;
}

/* Note when a job starts, so preemption can take the youngest first and so the
 * grace timer can tell whether the classical job got going on its own. */
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

static const struct flux_plugin_handler handlers[] = {
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


    if (!(g_jobs = json_object ()))
        return -1;

    if (flux_plugin_conf_unpack (p, "{s?s s?s s?i s?i s?F}",
                                 "vendors", &vendors,
                                 "protect_types", &protect_types,
                                 "total_cores", &total,
                                 "reserve_cores", &reserve,
                                 "preempt_after", &preempt_after) == 0) {
        if (vendors)
            g_vendors = strdup (vendors);
        if (protect_types)
            g_protect_types = strdup (protect_types);
        g_total_cores = total;
        g_reserve_cores = reserve;
        g_preempt_after = preempt_after;
        if (g_preempt_after > 0.0)
            flux_log (flux_jobtap_get_flux (p), LOG_WARNING,
                      "quantum: preemption is on. An unprotected job may be "
                      "cancelled to make room for a pair whose session is "
                      "already open, and a cancelled job loses its work");
    }

    /* registers the table and sets the plugin name in one call */
    return flux_plugin_register (p, "quantum", handlers);
}

/* vi: ts=4 sw=4 expandtab
 */
