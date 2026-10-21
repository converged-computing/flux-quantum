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
 * that were admitted but have not started. On top of that the scout needs a
 * core that is free right now, not one that preemption could free, because
 * nothing is preempted until the scout runs and releases its partner. The
 * job table behind this is built in job.new, which the job manager replays
 * on restart and reload.
 *
 * Priority. Both halves of a pair are given boost_priority, the scout one
 * more, so the scout is placed before its classical reserves and the
 * released classical is first in line for whatever comes free. That is the
 * plain job manager priority, the one a site without flux-accounting runs
 * on. A site with flux-accounting sets its own priorities and must load
 * with boost_priority=0, and then nothing orders the scout ahead of its
 * classical. That wants a factor inside flux-accounting, not this plugin.
 *
 * Preemption. The plugin subscribes to the classical half's events and
 * cancels other work only when three things hold. The scout memoed
 * released=1, so the session is open and the meter is running. A short
 * settle later the classical half is still not running. And fluxion's
 * sched annotation says it is reserved, put off to a later time, rather
 * than placed. With no annotation yet the plugin's own core table stands in
 * for the third. Then it cancels unprotected running jobs for the shortfall,
 * the classical's cores less what is free, lowest priority first and
 * youngest among equals, at most once per job. Cancelling is destructive,
 * so preemption is off unless preempt=1.
 *
 * Load with
 *   flux jobtap load quantum.so vendors=ibm,braket total_cores=128 \
 *       reserve_cores=32 protect_types=qpu preempt=1
 *
 * total_cores 0 disables admission control. protect_types names the resource
 * types that mark a job as one of ours, default qpu. settle is the seconds
 * between the release and the preemption decision, default 2. flux jobtap
 * query reports the configuration in force and the capacity numbers.
 */
#include <string.h>
#include <stdlib.h>
#include <stdint.h>
#include <errno.h>
#include <syslog.h>
#include <jansson.h>
#include <flux/core.h>
#include <flux/idset.h>
#include <flux/jobtap.h>

static char *g_vendors = NULL;         /* comma separated, default ibm,braket */
static char *g_protect_types = NULL;   /* comma separated, default qpu */
static int g_preempt = 0;              /* 0 disables preemption */
static double g_settle = 2.0;          /* seconds from release to the decision */
static int g_boost_priority = 1000;    /* priority of a pair, 0 leaves it alone */
static int g_total_cores = 0;          /* 0 disables admission control */
static int g_reserve_cores = 0;

/* every job the plugin has seen, keyed by jobid, with its cores, whether it
 * is protected, whether it is the classical half of a pair, the time it
 * started or 0, and for a classical half whether the scout released it,
 * whether preemption was already tried for it, and fluxion's latest start
 * estimate or 0 */
static json_t *g_jobs = NULL;

/* Not under quantum on purpose, so a plugin protecting some other scarce
 * resource can use the same key. The value is a reason and only presence
 * matters. */
static const char *PROTECT_KEY = "attributes.system.protected";

static int in_list (const char *list, const char *item)
{
    size_t ilen = strlen (item);
    const char *p = list;
    while (*p) {
        const char *comma = strchr (p, ',');
        size_t seg = comma ? (size_t)(comma - p) : strlen (p);
        if (seg == ilen && strncmp (p, item, ilen) == 0)
            return 1;
        if (!comma)
            break;
        p = comma + 1;
    }
    return 0;
}

static int type_protected (const char *type)
{
    return in_list (g_protect_types ? g_protect_types : "qpu", type);
}

static int vendor_allowed (const char *vendor)
{
    return in_list (g_vendors ? g_vendors : "ibm,braket", vendor);
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
    entry = json_pack ("{s:i s:b s:b s:f s:b s:b s:f s:s}",
                       "cores", cores, "protected", protected_,
                       "pair", pair, "run", run,
                       "released", 0, "preempted", 0, "t_estimate", 0.0,
                       "selection", "");
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

static int entry_flag (json_t *entry, const char *name)
{
    return json_is_true (json_object_get (entry, name));
}

static void entry_set_flag (json_t *entry, const char *name, int value)
{
    (void) json_object_set_new (entry, name, json_boolean (value));
}

static double entry_real (json_t *entry, const char *name)
{
    return json_number_value (json_object_get (entry, name));
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

/* Cores a job actually holds, from its R. The jobspec says what was asked
 * for and the scheduler may hand out more, for example a whole node, so once
 * R exists it is what the table counts. 0 when R is absent or unreadable. */
static int count_R_cores (json_t *R)
{
    json_t *rlite = NULL;
    json_t *item;
    size_t i;
    int total = 0;

    if (!R
        || json_unpack (R, "{s:{s:o}}", "execution", "R_lite", &rlite) < 0
        || !json_is_array (rlite))
        return 0;
    json_array_foreach (rlite, i, item) {
        const char *ranks = NULL, *cores = NULL;
        struct idset *rset, *cset;

        if (json_unpack (item, "{s:s s:{s:s}}",
                         "rank", &ranks, "children", "core", &cores) < 0)
            continue;
        if (!(rset = idset_decode (ranks)))
            continue;
        if ((cset = idset_decode (cores))) {
            total += (int) (idset_count (rset) * idset_count (cset));
            idset_destroy (cset);
        }
        idset_destroy (rset);
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
        /* The scout has to run before anything can be preempted for the
         * pair, so it needs a core that is free now. */
        if (freec - promised < 1)
            return flux_jobtap_reject_job (p, args,
                       "quantum: no free core for the scout. %d cores are "
                       "free and %d of those are promised to pairs that have "
                       "not started. Preemption cannot help, nothing is "
                       "preempted until the scout runs. Wait for a core to "
                       "free up",
                       freec, promised);
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

/* The job manager's priority for a job, or -1 when it cannot be read. */
static int64_t job_priority (flux_plugin_t *p, flux_jobid_t id)
{
    flux_plugin_arg_t *args;
    int64_t priority = -1;

    if (!(args = flux_jobtap_job_lookup (p, id)))
        return -1;
    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN,
                                "{s:I}", "priority", &priority) < 0)
        priority = -1;
    flux_plugin_arg_destroy (args);
    return priority;
}

/* Cancel unprotected running jobs until need cores are freed. The lowest
 * priority goes first, and among equals the youngest, since it has the least
 * work to lose. Returns the cores freed, which may be less than asked for if
 * there is not enough unprotected work to take. */
static int preempt_for (flux_plugin_t *p, flux_jobid_t sparing, int need)
{
    flux_t *h = flux_jobtap_get_flux (p);
    int freed = 0;

    while (freed < need) {
        const char *key, *chosen = NULL;
        json_t *entry;
        double newest = -1.0;
        int64_t lowest = 0;
        int cores = 0;

        json_object_foreach (g_jobs, key, entry) {
            int prot = 0, c = 0;
            double run = 0.0;
            int64_t prio;
            char spare[32];

            if (json_unpack (entry, "{s:i s:b s:F}",
                             "cores", &c, "protected", &prot, "run", &run) < 0)
                continue;
            if (prot || run <= 0.0 || c <= 0)
                continue;   /* protected, or not running, so not a candidate */
            if (strcmp (key, idkey (sparing, spare, sizeof (spare))) == 0)
                continue;
            prio = job_priority (p, (flux_jobid_t) strtoull (key, NULL, 10));
            if (!chosen
                || prio < lowest
                || (prio == lowest && run > newest)) {
                lowest = prio;
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
                                         "preempted to free %d cores for "
                                         "quantum job %ju, whose vendor "
                                         "session is already open",
                                         need, (uintmax_t) sparing) < 0) {
            flux_log_error (h, "quantum: could not preempt job %s", chosen);
            break;
        }
        flux_log (h, LOG_WARNING,
                  "quantum: preempted job %s (priority %jd) for %d cores",
                  chosen, (intmax_t) lowest, cores);
        freed += cores;
        /* it is going away, so do not pick it again on the next pass */
        (void) json_object_del (g_jobs, chosen);
    }
    return freed;
}

/* Pending settle timers are kept on a list so unloading the plugin can
 * destroy them. One firing after unload would take the job manager down. */
struct settle {
    flux_plugin_t *p;
    flux_jobid_t id;
    flux_watcher_t *w;
    struct settle *next;
};

static struct settle *g_settles = NULL;

static void settle_destroy (struct settle *s)
{
    struct settle **pp;

    for (pp = &g_settles; *pp; pp = &(*pp)->next) {
        if (*pp == s) {
            *pp = s->next;
            break;
        }
    }
    flux_watcher_destroy (s->w);
    free (s);
}

/* The release has had time to settle. Three things have to be true before
 * anything is cancelled: the scout released the job, the job is still not
 * running, and the scheduler says it could not place it. The last one is
 * fluxion's sched.selection_type annotation, "reserved" meaning it was put
 * off to a later time. With no annotation at all the plugin's own table
 * stands in, and a shortfall there is taken as the same thing. */
static void settle_cb (flux_reactor_t *r,
                       flux_watcher_t *w,
                       int revents,
                       void *arg)
{
    struct settle *s = arg;
    flux_t *h = flux_jobtap_get_flux (s->p);
    json_t *entry = lookup (s->id);
    int cores = 0, freec = 0, preemptible = 0, promised = 0, shortfall;
    const char *selection;

    if (!entry)
        goto done;      /* finished or vanished */
    if (entry_real (entry, "run") > 0.0)
        goto done;      /* started on its own, nothing to do */
    if (entry_flag (entry, "preempted"))
        goto done;      /* only once per job */
    if (json_unpack (entry, "{s:i}", "cores", &cores) < 0 || cores <= 0)
        goto done;

    capacity (&freec, &preemptible, &promised);
    shortfall = cores - freec;
    selection = json_string_value (json_object_get (entry, "selection"));
    if (!selection)
        selection = "";

    if (strcmp (selection, "reserved") == 0) {
        /* fluxion has put it off, and that is the word that counts */
        if (shortfall < 1) {
            flux_log (h, LOG_WARNING,
                      "quantum: job %ju is reserved by the scheduler but the "
                      "core table shows %d free for its %d, the table is "
                      "behind, preempting one job",
                      (uintmax_t) s->id, freec, cores);
            shortfall = 1;
        }
    }
    else if (*selection) {
        /* immediate, so it has been allocated and the run event is on its way */
        flux_log (h, LOG_INFO,
                  "quantum: job %ju was released %.0fs ago and the scheduler "
                  "has placed it (%s)",
                  (uintmax_t) s->id, g_settle, selection);
        goto done;
    }
    else if (shortfall <= 0) {
        /* no word from the scheduler and the table says it fits */
        flux_log (h, LOG_WARNING,
                  "quantum: job %ju was released %.0fs ago and is not running "
                  "with %d cores free and no scheduler annotation, leaving it "
                  "to the scheduler",
                  (uintmax_t) s->id, g_settle, freec);
        goto done;
    }
    flux_log (h, LOG_WARNING,
              "quantum: job %ju was released %.0fs ago and is not running, "
              "it needs %d cores and %d are free, preempting for %d "
              "(scheduler says %s)",
              (uintmax_t) s->id, g_settle, cores, freec, shortfall,
              *selection ? selection : "nothing");
    entry_set_flag (entry, "preempted", 1);
    (void) preempt_for (s->p, s->id, shortfall);
done:
    settle_destroy (s);
}

static void settle_arm (flux_plugin_t *p, flux_jobid_t id)
{
    flux_t *h = flux_jobtap_get_flux (p);
    struct settle *s;

    if (!(s = calloc (1, sizeof (*s))))
        return;
    s->p = p;
    s->id = id;
    if (!(s->w = flux_timer_watcher_create (flux_get_reactor (h),
                                            g_settle, 0.,
                                            settle_cb, s))) {
        free (s);
        return;
    }
    s->next = g_settles;
    g_settles = s;
    flux_watcher_start (s->w);
}

/* Events of the classical half of a pair, which the plugin subscribed to in
 * job.new. The scout's memo says the session is open. The scheduler's
 * annotation says when it thinks the job will start. */
static int event_cb (flux_plugin_t *p,
                     const char *topic,
                     flux_plugin_arg_t *args,
                     void *arg)
{
    flux_jobid_t id;
    const char *name = NULL;
    json_t *context = NULL;
    json_t *entry;

    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN,
                                "{s:I s:{s:s s?o}}",
                                "id", &id,
                                "entry", "name", &name, "context", &context) < 0)
        return 0;
    if (!(entry = lookup (id)) || !entry_flag (entry, "pair"))
        return 0;

    if (strcmp (name, "memo") == 0) {
        json_t *released = context ? json_object_get (context, "released") : NULL;
        if (!released || entry_flag (entry, "released"))
            return 0;
        entry_set_flag (entry, "released", 1);
        if (g_preempt && entry_real (entry, "run") <= 0.0)
            settle_arm (p, id);
    }
    else if (strcmp (name, "annotations") == 0) {
        double t_estimate = 0.0;
        const char *selection = "";
        if (context)
            (void) json_unpack (context, "{s:{s:{s?F s?s}}}",
                                "annotations", "sched",
                                "t_estimate", &t_estimate,
                                "selection_type", &selection);
        (void) json_object_set_new (entry, "t_estimate", json_real (t_estimate));
        (void) json_object_set_new (entry, "selection", json_string (selection));
    }
    return 0;
}

/* called for new jobs, and replayed for active ones on restart and reload */
static int new_cb (flux_plugin_t *p,
                   const char *topic,
                   flux_plugin_arg_t *args,
                   void *arg)
{
    json_t *jobspec = NULL;
    json_t *R = NULL;
    flux_jobid_t id;
    const char *vendor;
    int state = 0;
    double t_submit = 0.0;
    double run = 0.0;
    int cores;

    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN,
                                "{s:I s:o s?i s?F s?o}",
                                "id", &id,
                                "jobspec", &jobspec,
                                "state", &state,
                                "t_submit", &t_submit,
                                "R", &R) < 0)
        return 0;
    vendor = job_vendor (jobspec);

    /* job.state.run is not replayed, so a job already running on replay
     * would otherwise look idle. The start time is not in the args either, so
     * the submit time stands in and keeps the youngest first order. */
    if (state == FLUX_JOB_STATE_RUN || state == FLUX_JOB_STATE_CLEANUP)
        run = t_submit > 0.0 ? t_submit : 1.0;

    /* Every job is tracked, since any unprotected one is a preemption
     * candidate. Each entry holds what that job alone asks for, or holds
     * once R exists. The scout has its own entry, so the classical is not
     * charged for it. */
    cores = count_R_cores (R);
    if (cores <= 0)
        cores = count_tree_cores_top (jobspec);
    track (id, cores, needs_protection (jobspec, vendor), vendor ? 1 : 0, run);

    /* The classical half's release and reservation arrive as events. A
     * release that happened before a reload is not replayed, so that pair is
     * left to the scheduler. */
    if (g_preempt && vendor && run <= 0.0)
        (void) flux_jobtap_job_subscribe (p, id);
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
    json_t *R = NULL;
    json_t *entry;
    int cores;

    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN,
                                "{s:I s?o}", "id", &id, "R", &R) < 0)
        return 0;
    if ((entry = lookup (id))) {
        (void) json_object_set_new (entry, "run",
                                    json_real (flux_reactor_now (
                                        flux_get_reactor (
                                            flux_jobtap_get_flux (p)))));
        if ((cores = count_R_cores (R)) > 0)
            (void) json_object_set_new (entry, "cores", json_integer (cores));
    }
    /* running, so there is nothing more to watch for */
    flux_jobtap_job_unsubscribe (p, id);
    return 0;
}

/* Both halves of a pair go to the front of the line, and the scout one
 * place ahead of its classical. The scheduler takes pending jobs in priority
 * order, and the held classical reserves cores it is offered. With the two
 * level the classical would be first, its reservation would take the last
 * free core, and the scout behind it would never run to release it. The
 * builtin default runs first on the stack and sets priority to urgency,
 * which stands for everything else. */
static int priority_cb (flux_plugin_t *p,
                        const char *topic,
                        flux_plugin_arg_t *args,
                        void *arg)
{
    flux_jobid_t id;
    json_t *entry;
    json_int_t priority;

    if (g_boost_priority <= 0)
        return 0;
    if (flux_plugin_arg_unpack (args, FLUX_PLUGIN_ARG_IN, "{s:I}", "id", &id) < 0)
        return 0;
    if (!(entry = lookup (id)) || !entry_flag (entry, "protected"))
        return 0;
    priority = g_boost_priority + (entry_flag (entry, "pair") ? 0 : 1);
    if (flux_plugin_arg_pack (args, FLUX_PLUGIN_ARG_OUT,
                              "{s:I}", "priority", priority) < 0)
        flux_log_error (flux_jobtap_get_flux (p),
                        "quantum: could not set the priority of job %ju",
                        (uintmax_t) id);
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
    int settles = 0;
    struct settle *s;

    for (s = g_settles; s; s = s->next)
        settles++;
    capacity (&freec, &preemptible, &promised);
    if (flux_plugin_arg_pack (args,
                              FLUX_PLUGIN_ARG_OUT,
                              "{s:i s:i s:b s:f s:i s:s s:s s:i s:i s:i s:i s:i}",
                              "total_cores", g_total_cores,
                              "reserve_cores", g_reserve_cores,
                              "preempt", g_preempt,
                              "settle", g_settle,
                              "boost_priority", g_boost_priority,
                              "vendors", g_vendors ? g_vendors : "",
                              "protect_types", g_protect_types ? g_protect_types : "",
                              "free_cores", freec,
                              "preemptible_cores", preemptible,
                              "promised_cores", promised,
                              "tracked_jobs", g_jobs ? (int) json_object_size (g_jobs) : 0,
                              "pending_decisions", settles)
        < 0)
        return -1;
    return 0;
}

static void plugin_destroy (void *arg)
{
    while (g_settles)
        settle_destroy (g_settles);
    json_decref (g_jobs);
    g_jobs = NULL;
    free (g_vendors);
    g_vendors = NULL;
    free (g_protect_types);
    g_protect_types = NULL;
}

static const struct flux_plugin_handler handlers[] = {
    { "plugin.query",          query_cb,    NULL },
    { "job.validate",          validate_cb, NULL },
    { "job.new",               new_cb,      NULL },
    { "job.state.priority",    priority_cb, NULL },
    { "job.priority.get",      priority_cb, NULL },
    { "job.state.run",         run_cb,      NULL },
    { "job.event.memo",        event_cb,    NULL },
    { "job.event.annotations", event_cb,    NULL },
    { "job.destroy",           destroy_cb,  NULL },
    { 0 },
};

/* a flag given as 1 or true on the command line, or in toml */
static int flag_value (json_t *o)
{
    if (json_is_true (o))
        return 1;
    if (json_is_integer (o))
        return json_integer_value (o) != 0;
    return 0;
}

int flux_plugin_init (flux_plugin_t *p)
{
    const char *vendors = NULL;
    const char *protect_types = NULL;
    int total = 0, reserve = 0, boost = g_boost_priority;
    double settle = g_settle;
    json_t *preempt = NULL;
    json_t *preempt_after = NULL;
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
    rc = flux_plugin_conf_unpack (p, "{s?s s?s s?i s?i s?o s?F s?i s?o}",
                                  "vendors", &vendors,
                                  "protect_types", &protect_types,
                                  "total_cores", &total,
                                  "reserve_cores", &reserve,
                                  "preempt", &preempt,
                                  "settle", &settle,
                                  "boost_priority", &boost,
                                  "preempt_after", &preempt_after);
    if (rc < 0 && errno != ENOENT) {
        flux_log (h, LOG_ERR,
                  "quantum: cannot read plugin config: %s. Not loading, "
                  "because loading with defaults would leave admission control "
                  "and preemption off without saying so",
                  flux_plugin_strerror (p));
        return -1;
    }
    if (preempt_after) {
        flux_log (h, LOG_ERR,
                  "quantum: preempt_after is gone. Preemption now follows the "
                  "scout's release instead of a clock. Load with preempt=1");
        return -1;
    }
    if (rc == 0) {
        if (vendors)
            g_vendors = strdup (vendors);
        if (protect_types)
            g_protect_types = strdup (protect_types);
        g_total_cores = total;
        g_reserve_cores = reserve;
        g_preempt = flag_value (preempt);
        g_settle = settle > 0.0 ? settle : 2.0;
        g_boost_priority = boost > 0 ? boost : 0;
    }

    flux_log (h, LOG_INFO,
              "quantum: vendors=%s total_cores=%d reserve_cores=%d "
              "protect_types=%s preempt=%d settle=%.1f boost_priority=%d",
              g_vendors ? g_vendors : "(default)",
              g_total_cores,
              g_reserve_cores,
              g_protect_types ? g_protect_types : "(default)",
              g_preempt,
              g_settle,
              g_boost_priority);
    if (g_total_cores <= 0)
        flux_log (h, LOG_WARNING,
                  "quantum: total_cores is not set, so admission control is "
                  "off and a pair may be admitted that cannot be placed");
    if (g_preempt)
        flux_log (h, LOG_WARNING,
                  "quantum: preemption is on. An unprotected job may be "
                  "cancelled to make room for a pair whose session is "
                  "already open, and a cancelled job loses its work");
    else
        flux_log (h, LOG_WARNING,
                  "quantum: preemption is off. A pair that cannot be placed "
                  "will hold an open vendor session until the machine frees "
                  "up on its own");
    if (g_boost_priority > 0)
        flux_log (h, LOG_INFO,
                  "quantum: pairs run at priority %d. A site with "
                  "flux-accounting should load with boost_priority=0",
                  g_boost_priority);

    if (flux_plugin_register (p, "quantum", handlers) < 0) {
        flux_log_error (h, "quantum: could not register handlers");
        return -1;
    }

    return 0;
}

/* vi: ts=4 sw=4 expandtab
 */
