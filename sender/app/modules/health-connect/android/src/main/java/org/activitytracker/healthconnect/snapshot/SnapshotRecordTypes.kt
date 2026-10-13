package org.activitytracker.healthconnect.snapshot

import androidx.health.connect.client.feature.ExperimentalMindfulnessSessionApi
import androidx.health.connect.client.records.*
import java.time.Instant
import kotlin.reflect.KClass

/** Explicit bindings to the pinned SDK; uses only public record properties. */
@OptIn(ExperimentalMindfulnessSessionApi::class)
internal object SnapshotRecordTypes {
    val classes: Map<String, KClass<out Record>> = mapOf(
        "active-calories-burned" to ActiveCaloriesBurnedRecord::class,
        "basal-body-temperature" to BasalBodyTemperatureRecord::class,
        "basal-metabolic-rate" to BasalMetabolicRateRecord::class,
        "blood-glucose" to BloodGlucoseRecord::class,
        "blood-pressure" to BloodPressureRecord::class,
        "body-fat" to BodyFatRecord::class,
        "body-temperature" to BodyTemperatureRecord::class,
        "body-water-mass" to BodyWaterMassRecord::class,
        "bone-mass" to BoneMassRecord::class,
        "cervical-mucus" to CervicalMucusRecord::class,
        "cycling-pedaling-cadence" to CyclingPedalingCadenceRecord::class,
        "distance" to DistanceRecord::class,
        "elevation-gained" to ElevationGainedRecord::class,
        "exercise-session" to ExerciseSessionRecord::class,
        "floors-climbed" to FloorsClimbedRecord::class,
        "heart-rate" to HeartRateRecord::class,
        "heart-rate-variability-rmssd" to HeartRateVariabilityRmssdRecord::class,
        "height" to HeightRecord::class,
        "hydration" to HydrationRecord::class,
        "intermenstrual-bleeding" to IntermenstrualBleedingRecord::class,
        "lean-body-mass" to LeanBodyMassRecord::class,
        "menstruation-flow" to MenstruationFlowRecord::class,
        "menstruation-period" to MenstruationPeriodRecord::class,
        "mindfulness-session" to MindfulnessSessionRecord::class,
        "nutrition" to NutritionRecord::class,
        "ovulation-test" to OvulationTestRecord::class,
        "oxygen-saturation" to OxygenSaturationRecord::class,
        "planned-exercise-session" to PlannedExerciseSessionRecord::class,
        "power" to PowerRecord::class,
        "respiratory-rate" to RespiratoryRateRecord::class,
        "resting-heart-rate" to RestingHeartRateRecord::class,
        "sexual-activity" to SexualActivityRecord::class,
        "skin-temperature" to SkinTemperatureRecord::class,
        "sleep-session" to SleepSessionRecord::class,
        "speed" to SpeedRecord::class,
        "steps" to StepsRecord::class,
        "steps-cadence" to StepsCadenceRecord::class,
        "total-calories-burned" to TotalCaloriesBurnedRecord::class,
        "weight" to WeightRecord::class,
        "vo2-max" to Vo2MaxRecord::class,
        "wheelchair-pushes" to WheelchairPushesRecord::class,
    )

    fun start(record: Record): Instant = when (record) {
        is ActiveCaloriesBurnedRecord -> record.startTime
        is BasalBodyTemperatureRecord -> record.time
        is BasalMetabolicRateRecord -> record.time
        is BloodGlucoseRecord -> record.time
        is BloodPressureRecord -> record.time
        is BodyFatRecord -> record.time
        is BodyTemperatureRecord -> record.time
        is BodyWaterMassRecord -> record.time
        is BoneMassRecord -> record.time
        is CervicalMucusRecord -> record.time
        is CyclingPedalingCadenceRecord -> record.startTime
        is DistanceRecord -> record.startTime
        is ElevationGainedRecord -> record.startTime
        is ExerciseSessionRecord -> record.startTime
        is FloorsClimbedRecord -> record.startTime
        is HeartRateRecord -> record.startTime
        is HeartRateVariabilityRmssdRecord -> record.time
        is HeightRecord -> record.time
        is HydrationRecord -> record.startTime
        is IntermenstrualBleedingRecord -> record.time
        is LeanBodyMassRecord -> record.time
        is MenstruationFlowRecord -> record.time
        is MenstruationPeriodRecord -> record.startTime
        is MindfulnessSessionRecord -> record.startTime
        is NutritionRecord -> record.startTime
        is OvulationTestRecord -> record.time
        is OxygenSaturationRecord -> record.time
        is PlannedExerciseSessionRecord -> record.startTime
        is PowerRecord -> record.startTime
        is RespiratoryRateRecord -> record.time
        is RestingHeartRateRecord -> record.time
        is SexualActivityRecord -> record.time
        is SkinTemperatureRecord -> record.startTime
        is SleepSessionRecord -> record.startTime
        is SpeedRecord -> record.startTime
        is StepsRecord -> record.startTime
        is StepsCadenceRecord -> record.startTime
        is TotalCaloriesBurnedRecord -> record.startTime
        is WeightRecord -> record.time
        is Vo2MaxRecord -> record.time
        is WheelchairPushesRecord -> record.startTime
        else -> error("Unsupported snapshot SDK record")
    }
}
