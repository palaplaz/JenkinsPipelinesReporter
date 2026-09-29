/*
 * FORCE_PASS audit collector.
 *
 * Executed by fpreporter via POST /scriptText. Read-only: it never modifies Jenkins.
 *
 * Input  (base64 JSON injected into INPUT_B64):
 *   { parameterName, sinceMs, lagMs, recheck: [ {job, number} ] }
 * Output (single JSON document printed between BEGIN/END markers):
 *   { schemaVersion, jenkinsVersion, parameterName, generatedAt, windowStart, windowEnd,
 *     scannedJobs, scannedBuilds, counts: [...], builds: [...], rechecked: [...] }
 *
 * The collection window is [sinceMs, now - lagMs), measured on the Jenkins clock, based on
 * each build's scheduled timestamp. Consecutive runs pass the previous windowEnd as sinceMs,
 * so windows never overlap and the true/false counts are exact.
 *
 * Pasted into the Script Console as-is (placeholder not replaced), it runs a
 * last-7-days scan with pretty-printed output for manual verification.
 */
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import hudson.model.Cause
import hudson.model.Job
import hudson.model.ParametersAction
import hudson.model.Run
import jenkins.model.Jenkins

final int SCHEMA_VERSION = 1
final String BEGIN_MARKER = '###FP_JSON_BEGIN###'
final String END_MARKER = '###FP_JSON_END###'

def INPUT_B64 = '@@FP_INPUT_B64@@'

boolean manualRun = INPUT_B64.startsWith('@@')
Map input = manualRun
    ? [parameterName: 'FORCE_PASS',
       sinceMs      : System.currentTimeMillis() - 7L * 24 * 60 * 60 * 1000,
       lagMs        : 5L * 60 * 1000,
       recheck      : []]
    : (Map) new JsonSlurper().parseText(new String(Base64.getDecoder().decode(INPUT_B64), 'UTF-8'))

String resultOf(Run build) {
    // A result can be set while the build is still running (e.g. a failed stage), so check isBuilding first.
    if (build.isBuilding()) {
        return 'RUNNING'
    }
    return build.getResult()?.toString() ?: 'UNKNOWN'
}

String urlOf(Run build) {
    try {
        return build.getAbsoluteUrl()
    } catch (IllegalStateException ignored) {
        // Jenkins root URL is not configured; fall back to the relative URL.
        return build.getUrl()
    }
}

Map authorOf(Run build) {
    List causes = (build.getCauses() ?: []).findAll { it != null }
    def userCause = causes.find { it instanceof Cause.UserIdCause }
    if (userCause) {
        return [id: userCause.getUserId() ?: 'unknown', name: userCause.getUserName() ?: 'unknown']
    }
    // Triggered automatically: timer, SCM, upstream build, ...
    def first = causes ? causes[0] : null
    return [id: 'SYSTEM/AUTOMATED', name: first ? first.getShortDescription() : 'UNKNOWN']
}

String causesOf(Run build) {
    return (build.getCauses() ?: []).findAll { it != null }.collect { it.getShortDescription() }.join('; ')
}

Map parametersOf(ParametersAction action) {
    Map out = [:]
    action.getParameters().each { pv ->
        def value = pv.getValue()
        out[pv.getName()] = pv.isSensitive() ? '****' : (value == null ? null : value.toString())
    }
    return out
}

long now = System.currentTimeMillis()
String paramName = input.parameterName as String
long since = input.sinceMs as long
long until = Math.max(since, now - (input.lagMs as long))

Map counts = [:]   // job full name -> [trueCount, falseCount]
List builds = []
int scannedJobs = 0
int scannedBuilds = 0

for (Job job : Jenkins.get().allItems(Job.class)) {
    // Matrix configurations and similar child jobs copy their parent's parameters; counting them would double count.
    if (job.getParent() instanceof Job) {
        continue
    }
    scannedJobs++
    try {
        // Builds are ordered newest first, so stop at the first one older than the window.
        for (Run build : job.getBuilds()) {
            long timestamp = build.getTimeInMillis()
            if (timestamp >= until) {
                continue
            }
            if (timestamp < since) {
                break
            }
            scannedBuilds++

            ParametersAction action = build.getAction(ParametersAction.class)
            def parameter = action?.getParameter(paramName)
            if (parameter == null) {
                continue
            }
            String value = parameter.getValue()?.toString()?.trim()
            boolean isTrue = 'true'.equalsIgnoreCase(value)
            boolean isFalse = 'false'.equalsIgnoreCase(value)
            if (!isTrue && !isFalse) {
                continue
            }

            Map jobCounts = counts.get(job.getFullName())
            if (jobCounts == null) {
                jobCounts = [trueCount: 0, falseCount: 0]
                counts.put(job.getFullName(), jobCounts)
            }
            if (isFalse) {
                jobCounts.falseCount++
                continue
            }
            jobCounts.trueCount++

            Map author = authorOf(build)
            builds << [
                job       : job.getFullName(),
                number    : build.getNumber(),
                result    : resultOf(build),
                startMs   : timestamp,
                url       : urlOf(build),
                value     : value,
                authorId  : author.id,
                authorName: author.name,
                cause     : causesOf(build),
                parameters: parametersOf(action),
            ]
        }
    } catch (Exception e) {
        // Fail the whole run: skipping a job would silently lose its builds once the watermark advances.
        throw new RuntimeException("Failed while scanning job '" + job.getFullName() + "'", e)
    }
}

List rechecked = (input.recheck ?: []).collect { Map r ->
    Job job = Jenkins.get().getItemByFullName(r.job as String, Job.class)
    Run build = job?.getBuildByNumber(r.number as int)
    [job: r.job, number: r.number as int, result: build == null ? 'NOT_FOUND' : resultOf(build)]
}

Map payload = [
    schemaVersion : SCHEMA_VERSION,
    jenkinsVersion: Jenkins.VERSION,
    parameterName : paramName,
    generatedAt   : now,
    windowStart   : since,
    windowEnd     : until,
    scannedJobs   : scannedJobs,
    scannedBuilds : scannedBuilds,
    counts        : counts.collect { job, c -> [job: job, trueCount: c.trueCount, falseCount: c.falseCount] },
    builds        : builds,
    rechecked     : rechecked,
]

String json = JsonOutput.toJson(payload)
if (manualRun) {
    println("Manual run (placeholder not replaced): scanned builds of the last 7 days for ${paramName}.")
    json = JsonOutput.prettyPrint(json)
}
println(BEGIN_MARKER)
println(json)
println(END_MARKER)
return null
