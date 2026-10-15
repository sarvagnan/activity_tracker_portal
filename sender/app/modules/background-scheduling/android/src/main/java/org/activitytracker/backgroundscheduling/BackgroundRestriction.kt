package org.activitytracker.backgroundscheduling

/** No Android dependency: either restriction blocks deferrable background work.
 * Read both sources and let failures escape to the TypeScript observation.
 */
internal fun backgroundRestricted(
    activityRestricted: () -> Boolean,
    standbyBucket: () -> Int,
    restrictedBucket: Int,
): Boolean {
    val activity = activityRestricted()
    val bucket = standbyBucket()
    return activity || bucket == restrictedBucket
}
