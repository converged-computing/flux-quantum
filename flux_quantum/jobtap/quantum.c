/*
 * quantum-jobtap: a Flux job-manager jobtap plugin (policy layer) for
 * quantum + classical coscheduling.
 *
 * External-quota model: a quantum job is identified by the attribute
 *   attributes.system.quantum.vendor = "<vendor>"
 * (set by `flux quantum-submit`). This plugin enforces SITE POLICY on such
 * jobs at job.validate: it permits only configured vendors and rejects others
 * before submission completes. It consumes no resources and submits nothing.
 *
 * Load:   flux jobtap load /path/quantum-jobtap.so [vendors="ibm,braket"]
 * List:   flux jobtap list
 *
 * Extension points (deliberately NOT enabled here; see NOTES):
 *   - central hold enforcement (set attributes.system.hold from the plugin)
 *   - owned-hardware resource scoping (rewrite qpu -> qvendor_<v> -> qpu)
 * Both depend on which jobspec-update keys the job-manager permits, which must
 * be confirmed on the target flux-core before relying on them. The reliable
 * home for the hold today is `flux quantum-submit` (--setattr=system.hold=1),
 * which is already proven.
 */
#include <string.h>
#include <stdlib.h>
#include <jansson.h>
#include <flux/core.h>
#include <flux/jobtap.h>

/* default vendor allowlist; over/replace via load-time config "vendors=" */
static char *g_vendors = NULL;   /* comma-separated, e.g. "ibm,braket" */

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

    /* attributes.system.quantum.vendor -- absent => not a quantum job */
    (void) json_unpack (jobspec, "{s:{s:{s:{s:s}}}}",
                        "attributes", "system", "quantum", "vendor", &vendor);
    if (!vendor)
        return 0;

    if (!vendor_allowed (vendor))
        return flux_jobtap_reject_job (p, args,
                   "quantum: vendor '%s' is not permitted at this site", vendor);

    /* policy satisfied; the job proceeds. The hold is set at submission by
     * flux quantum-submit, and the scout unholds it once the session is live. */
    return 0;
}

int flux_plugin_init (flux_plugin_t *p)
{
    const char *vendors = NULL;

    if (flux_plugin_set_name (p, "quantum") < 0)
        return -1;

    /* optional load-time config: vendors="ibm,braket" */
    if (flux_plugin_conf_unpack (p, "{s?s}", "vendors", &vendors) == 0
        && vendors)
        g_vendors = strdup (vendors);

    return flux_plugin_add_handler (p, "job.validate", validate_cb, NULL);
}

/* vi: ts=4 sw=4 expandtab
 */
