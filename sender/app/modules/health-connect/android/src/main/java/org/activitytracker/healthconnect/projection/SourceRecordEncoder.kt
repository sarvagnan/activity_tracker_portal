package org.activitytracker.healthconnect.projection

import androidx.health.connect.client.feature.ExperimentalMindfulnessSessionApi
import androidx.health.connect.client.records.*
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.records.metadata.Device
import androidx.health.connect.client.records.metadata.Metadata
import java.util.Collections

/** Typed field access for the pinned SDK. Field statements follow the frozen model order. */
@OptIn(ExperimentalMindfulnessSessionApi::class)
internal object SourceRecordEncoder {
    val schemaVersions: Map<String, Int> = Collections.unmodifiableMap(mapOf(
        "active-calories-burned" to 1,
        "basal-body-temperature" to 1,
        "basal-metabolic-rate" to 1,
        "blood-glucose" to 1,
        "blood-pressure" to 1,
        "body-fat" to 1,
        "body-temperature" to 1,
        "body-water-mass" to 1,
        "bone-mass" to 1,
        "cervical-mucus" to 1,
        "cycling-pedaling-cadence" to 1,
        "distance" to 1,
        "elevation-gained" to 1,
        "exercise-session" to 1,
        "floors-climbed" to 1,
        "heart-rate" to 1,
        "heart-rate-variability-rmssd" to 1,
        "height" to 1,
        "hydration" to 1,
        "intermenstrual-bleeding" to 1,
        "lean-body-mass" to 1,
        "menstruation-flow" to 1,
        "menstruation-period" to 1,
        "mindfulness-session" to 1,
        "nutrition" to 1,
        "ovulation-test" to 1,
        "oxygen-saturation" to 1,
        "planned-exercise-session" to 1,
        "power" to 1,
        "respiratory-rate" to 1,
        "resting-heart-rate" to 1,
        "sexual-activity" to 1,
        "skin-temperature" to 1,
        "sleep-session" to 1,
        "speed" to 1,
        "steps" to 1,
        "steps-cadence" to 1,
        "total-calories-burned" to 1,
        "weight" to 1,
        "vo2-max" to 1,
        "wheelchair-pushes" to 1,
    ))

    fun recordType(record: Record): String? = when (record) {
        is ActiveCaloriesBurnedRecord -> "active-calories-burned"
        is BasalBodyTemperatureRecord -> "basal-body-temperature"
        is BasalMetabolicRateRecord -> "basal-metabolic-rate"
        is BloodGlucoseRecord -> "blood-glucose"
        is BloodPressureRecord -> "blood-pressure"
        is BodyFatRecord -> "body-fat"
        is BodyTemperatureRecord -> "body-temperature"
        is BodyWaterMassRecord -> "body-water-mass"
        is BoneMassRecord -> "bone-mass"
        is CervicalMucusRecord -> "cervical-mucus"
        is CyclingPedalingCadenceRecord -> "cycling-pedaling-cadence"
        is DistanceRecord -> "distance"
        is ElevationGainedRecord -> "elevation-gained"
        is ExerciseSessionRecord -> "exercise-session"
        is FloorsClimbedRecord -> "floors-climbed"
        is HeartRateRecord -> "heart-rate"
        is HeartRateVariabilityRmssdRecord -> "heart-rate-variability-rmssd"
        is HeightRecord -> "height"
        is HydrationRecord -> "hydration"
        is IntermenstrualBleedingRecord -> "intermenstrual-bleeding"
        is LeanBodyMassRecord -> "lean-body-mass"
        is MenstruationFlowRecord -> "menstruation-flow"
        is MenstruationPeriodRecord -> "menstruation-period"
        is MindfulnessSessionRecord -> "mindfulness-session"
        is NutritionRecord -> "nutrition"
        is OvulationTestRecord -> "ovulation-test"
        is OxygenSaturationRecord -> "oxygen-saturation"
        is PlannedExerciseSessionRecord -> "planned-exercise-session"
        is PowerRecord -> "power"
        is RespiratoryRateRecord -> "respiratory-rate"
        is RestingHeartRateRecord -> "resting-heart-rate"
        is SexualActivityRecord -> "sexual-activity"
        is SkinTemperatureRecord -> "skin-temperature"
        is SleepSessionRecord -> "sleep-session"
        is SpeedRecord -> "speed"
        is StepsRecord -> "steps"
        is StepsCadenceRecord -> "steps-cadence"
        is TotalCaloriesBurnedRecord -> "total-calories-burned"
        is WeightRecord -> "weight"
        is Vo2MaxRecord -> "vo2-max"
        is WheelchairPushesRecord -> "wheelchair-pushes"
        else -> null
    }

    fun encode(record: Record, recordType: String, context: ProjectionContext): String {
        // Validate metadata first, then record fields, before sorting any object keys.
        val metadata = fields(record.metadata, "/metadata")
        val body = recordFields(record)
        return jsonObject("") {
            field("record_type", recordType, ::text)
            field("projection_schema_version", 1) { value, _ -> value.toString() }
            field("projection_implementation_version", context.implementationVersion, ::text)
            field("sdk_version", "androidx.health.connect:connect-client:1.1.0", ::text)
            field("provider_version", context.providerVersion, ::text)
            field("metadata", metadata) { value, _ -> value }
            field("record", body) { value, _ -> value }
        }
    }

    private fun recordFields(record: Record): String = when (record) {
        is ActiveCaloriesBurnedRecord -> fields(record, "/record")
        is BasalBodyTemperatureRecord -> fields(record, "/record")
        is BasalMetabolicRateRecord -> fields(record, "/record")
        is BloodGlucoseRecord -> fields(record, "/record")
        is BloodPressureRecord -> fields(record, "/record")
        is BodyFatRecord -> fields(record, "/record")
        is BodyTemperatureRecord -> fields(record, "/record")
        is BodyWaterMassRecord -> fields(record, "/record")
        is BoneMassRecord -> fields(record, "/record")
        is CervicalMucusRecord -> fields(record, "/record")
        is CyclingPedalingCadenceRecord -> fields(record, "/record")
        is DistanceRecord -> fields(record, "/record")
        is ElevationGainedRecord -> fields(record, "/record")
        is ExerciseSessionRecord -> fields(record, "/record")
        is FloorsClimbedRecord -> fields(record, "/record")
        is HeartRateRecord -> fields(record, "/record")
        is HeartRateVariabilityRmssdRecord -> fields(record, "/record")
        is HeightRecord -> fields(record, "/record")
        is HydrationRecord -> fields(record, "/record")
        is IntermenstrualBleedingRecord -> fields(record, "/record")
        is LeanBodyMassRecord -> fields(record, "/record")
        is MenstruationFlowRecord -> fields(record, "/record")
        is MenstruationPeriodRecord -> fields(record, "/record")
        is MindfulnessSessionRecord -> fields(record, "/record")
        is NutritionRecord -> fields(record, "/record")
        is OvulationTestRecord -> fields(record, "/record")
        is OxygenSaturationRecord -> fields(record, "/record")
        is PlannedExerciseSessionRecord -> fields(record, "/record")
        is PowerRecord -> fields(record, "/record")
        is RespiratoryRateRecord -> fields(record, "/record")
        is RestingHeartRateRecord -> fields(record, "/record")
        is SexualActivityRecord -> fields(record, "/record")
        is SkinTemperatureRecord -> fields(record, "/record")
        is SleepSessionRecord -> fields(record, "/record")
        is SpeedRecord -> fields(record, "/record")
        is StepsRecord -> fields(record, "/record")
        is StepsCadenceRecord -> fields(record, "/record")
        is TotalCaloriesBurnedRecord -> fields(record, "/record")
        is WeightRecord -> fields(record, "/record")
        is Vo2MaxRecord -> fields(record, "/record")
        is WheelchairPushesRecord -> fields(record, "/record")
        else -> error("Unsupported record reached the typed encoder")
    }

    private fun fields(value: Metadata, path: String): String = jsonObject(path) {
        field("id", value.id) { item, fieldPath -> text(item, fieldPath) }
        field("data_origin", value.dataOrigin) { item, fieldPath -> fields(item, fieldPath) }
        field("last_modified_time", value.lastModifiedTime) { item, fieldPath -> instant(item, fieldPath) }
        field("client_record_id", value.clientRecordId) { item, fieldPath -> text(item, fieldPath) }
        field("client_record_version", value.clientRecordVersion) { item, fieldPath -> text(item.toString(), fieldPath) }
        field("recording_method", value.recordingMethod) { item, fieldPath -> constant(item, RecordingMethod, fieldPath) }
        field("device", value.device) { item, fieldPath -> fields(item, fieldPath) }
    }

    private fun fields(value: DataOrigin, path: String): String = jsonObject(path) {
        field("package_name", value.packageName) { item, fieldPath -> text(item, fieldPath) }
    }

    private fun fields(value: Device, path: String): String = jsonObject(path) {
        field("type", value.type) { item, fieldPath -> constant(item, DeviceType, fieldPath) }
        field("manufacturer", value.manufacturer) { item, fieldPath -> text(item, fieldPath) }
        field("model", value.model) { item, fieldPath -> text(item, fieldPath) }
    }

    private fun fields(value: CyclingPedalingCadenceRecord.Sample, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("revolutions_per_minute", value.revolutionsPerMinute) { item, fieldPath -> number(item, fieldPath) }
    }

    private fun fields(value: HeartRateRecord.Sample, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("beats_per_minute", value.beatsPerMinute) { item, fieldPath -> item.toString() }
    }

    private fun fields(value: PowerRecord.Sample, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("power_watts", value.power) { item, fieldPath -> number(item.inWatts, fieldPath) }
    }

    private fun fields(value: SpeedRecord.Sample, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("speed_meters_per_second", value.speed) { item, fieldPath -> number(item.inMetersPerSecond, fieldPath) }
    }

    private fun fields(value: StepsCadenceRecord.Sample, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("rate", value.rate) { item, fieldPath -> number(item, fieldPath) }
    }

    private fun fields(value: SkinTemperatureRecord.Delta, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("delta_celsius", value.delta) { item, fieldPath -> number(item.inCelsius, fieldPath) }
    }

    private fun fields(value: SleepSessionRecord.Stage, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("stage", value.stage) { item, fieldPath -> constant(item, SleepStage, fieldPath) }
    }

    private fun fields(value: ExerciseSegment, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("segment_type", value.segmentType) { item, fieldPath -> constant(item, ExerciseSegmentType, fieldPath) }
        field("repetitions", value.repetitions) { item, fieldPath -> item.toString() }
    }

    private fun fields(value: ExerciseLap, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("length_meters", value.length) { item, fieldPath -> number(item.inMeters, fieldPath) }
    }

    private fun fields(value: ExerciseRoute, path: String): String = jsonObject(path) {
        field("route", value.route) { item, fieldPath -> array(item, fieldPath, ::fields) }
    }

    private fun fields(value: ExerciseRoute.Location, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("latitude", value.latitude) { item, fieldPath -> number(item, fieldPath) }
        field("longitude", value.longitude) { item, fieldPath -> number(item, fieldPath) }
        field("horizontal_accuracy_meters", value.horizontalAccuracy) { item, fieldPath -> number(item.inMeters, fieldPath) }
        field("vertical_accuracy_meters", value.verticalAccuracy) { item, fieldPath -> number(item.inMeters, fieldPath) }
        field("altitude_meters", value.altitude) { item, fieldPath -> number(item.inMeters, fieldPath) }
    }

    private fun fields(value: PlannedExerciseBlock, path: String): String = jsonObject(path) {
        field("repetitions", value.repetitions) { item, fieldPath -> item.toString() }
        field("steps", value.steps) { item, fieldPath -> array(item, fieldPath, ::fields) }
        field("description", value.description) { item, fieldPath -> text(item, fieldPath) }
    }

    private fun fields(value: PlannedExerciseStep, path: String): String = jsonObject(path) {
        field("exercise_type", value.exerciseType) { item, fieldPath -> constant(item, ExerciseSegmentType, fieldPath) }
        field("exercise_phase", value.exercisePhase) { item, fieldPath -> constant(item, ExercisePhase, fieldPath) }
        field("completion_goal", value.completionGoal) { item, fieldPath -> fields(item, fieldPath) }
        field("performance_targets", value.performanceTargets) { item, fieldPath -> array(item, fieldPath, ::fields) }
        field("description", value.description) { item, fieldPath -> text(item, fieldPath) }
    }

    private fun fields(value: ActiveCaloriesBurnedRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("energy_calories", value.energy) { item, fieldPath -> number(item.inCalories, fieldPath) }
    }

    private fun fields(value: BasalBodyTemperatureRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("temperature_celsius", value.temperature) { item, fieldPath -> number(item.inCelsius, fieldPath) }
        field("measurement_location", value.measurementLocation) { item, fieldPath -> constant(item, BodyTemperatureMeasurementLocation, fieldPath) }
    }

    private fun fields(value: BasalMetabolicRateRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("basal_metabolic_rate_watts", value.basalMetabolicRate) { item, fieldPath -> number(item.inWatts, fieldPath) }
    }

    private fun fields(value: BloodGlucoseRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("level_millimoles_per_liter", value.level) { item, fieldPath -> number(item.inMillimolesPerLiter, fieldPath) }
        field("specimen_source", value.specimenSource) { item, fieldPath -> constant(item, BloodGlucoseSpecimenSource, fieldPath) }
        field("meal_type", value.mealType) { item, fieldPath -> constant(item, MealType, fieldPath) }
        field("relation_to_meal", value.relationToMeal) { item, fieldPath -> constant(item, BloodGlucoseRelationToMeal, fieldPath) }
    }

    private fun fields(value: BloodPressureRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("systolic_millimeters_of_mercury", value.systolic) { item, fieldPath -> number(item.inMillimetersOfMercury, fieldPath) }
        field("diastolic_millimeters_of_mercury", value.diastolic) { item, fieldPath -> number(item.inMillimetersOfMercury, fieldPath) }
        field("body_position", value.bodyPosition) { item, fieldPath -> constant(item, BloodPressureBodyPosition, fieldPath) }
        field("measurement_location", value.measurementLocation) { item, fieldPath -> constant(item, BloodPressureMeasurementLocation, fieldPath) }
    }

    private fun fields(value: BodyFatRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("percentage", value.percentage) { item, fieldPath -> number(item.value, fieldPath) }
    }

    private fun fields(value: BodyTemperatureRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("temperature_celsius", value.temperature) { item, fieldPath -> number(item.inCelsius, fieldPath) }
        field("measurement_location", value.measurementLocation) { item, fieldPath -> constant(item, BodyTemperatureMeasurementLocation, fieldPath) }
    }

    private fun fields(value: BodyWaterMassRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("mass_grams", value.mass) { item, fieldPath -> number(item.inGrams, fieldPath) }
    }

    private fun fields(value: BoneMassRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("mass_grams", value.mass) { item, fieldPath -> number(item.inGrams, fieldPath) }
    }

    private fun fields(value: CervicalMucusRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("appearance", value.appearance) { item, fieldPath -> constant(item, CervicalMucusAppearance, fieldPath) }
        field("sensation", value.sensation) { item, fieldPath -> constant(item, CervicalMucusSensation, fieldPath) }
    }

    private fun fields(value: CyclingPedalingCadenceRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("samples", value.samples) { item, fieldPath -> array(item, fieldPath, ::fields) }
    }

    private fun fields(value: DistanceRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("distance_meters", value.distance) { item, fieldPath -> number(item.inMeters, fieldPath) }
    }

    private fun fields(value: ElevationGainedRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("elevation_meters", value.elevation) { item, fieldPath -> number(item.inMeters, fieldPath) }
    }

    private fun fields(value: ExerciseSessionRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("exercise_type", value.exerciseType) { item, fieldPath -> constant(item, ExerciseType, fieldPath) }
        field("title", value.title) { item, fieldPath -> text(item, fieldPath) }
        field("notes", value.notes) { item, fieldPath -> text(item, fieldPath) }
        field("segments", value.segments) { item, fieldPath -> array(item, fieldPath, ::fields) }
        field("laps", value.laps) { item, fieldPath -> array(item, fieldPath, ::fields) }
        field("exercise_route_result", value.exerciseRouteResult) { item, fieldPath -> fields(item, fieldPath) }
        field("planned_exercise_session_id", value.plannedExerciseSessionId) { item, fieldPath -> text(item, fieldPath) }
    }

    private fun fields(value: FloorsClimbedRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("floors", value.floors) { item, fieldPath -> number(item, fieldPath) }
    }

    private fun fields(value: HeartRateRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("samples", value.samples) { item, fieldPath -> array(item, fieldPath, ::fields) }
    }

    private fun fields(value: HeartRateVariabilityRmssdRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("heart_rate_variability_millis", value.heartRateVariabilityMillis) { item, fieldPath -> number(item, fieldPath) }
    }

    private fun fields(value: HeightRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("height_meters", value.height) { item, fieldPath -> number(item.inMeters, fieldPath) }
    }

    private fun fields(value: HydrationRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("volume_liters", value.volume) { item, fieldPath -> number(item.inLiters, fieldPath) }
    }

    private fun fields(value: IntermenstrualBleedingRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
    }

    private fun fields(value: LeanBodyMassRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("mass_grams", value.mass) { item, fieldPath -> number(item.inGrams, fieldPath) }
    }

    private fun fields(value: MenstruationFlowRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("flow", value.flow) { item, fieldPath -> constant(item, MenstruationFlow, fieldPath) }
    }

    private fun fields(value: MenstruationPeriodRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
    }

    private fun fields(value: MindfulnessSessionRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("mindfulness_session_type", value.mindfulnessSessionType) { item, fieldPath -> constant(item, MindfulnessSessionType, fieldPath) }
        field("title", value.title) { item, fieldPath -> text(item, fieldPath) }
        field("notes", value.notes) { item, fieldPath -> text(item, fieldPath) }
    }

    private fun fields(value: NutritionRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("energy_calories", value.energy) { item, fieldPath -> number(item.inCalories, fieldPath) }
        field("energy_from_fat_calories", value.energyFromFat) { item, fieldPath -> number(item.inCalories, fieldPath) }
        field("biotin_grams", value.biotin) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("caffeine_grams", value.caffeine) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("calcium_grams", value.calcium) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("chloride_grams", value.chloride) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("cholesterol_grams", value.cholesterol) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("chromium_grams", value.chromium) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("copper_grams", value.copper) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("dietary_fiber_grams", value.dietaryFiber) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("folate_grams", value.folate) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("folic_acid_grams", value.folicAcid) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("iodine_grams", value.iodine) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("iron_grams", value.iron) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("magnesium_grams", value.magnesium) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("manganese_grams", value.manganese) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("molybdenum_grams", value.molybdenum) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("monounsaturated_fat_grams", value.monounsaturatedFat) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("niacin_grams", value.niacin) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("pantothenic_acid_grams", value.pantothenicAcid) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("phosphorus_grams", value.phosphorus) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("polyunsaturated_fat_grams", value.polyunsaturatedFat) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("potassium_grams", value.potassium) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("protein_grams", value.protein) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("riboflavin_grams", value.riboflavin) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("saturated_fat_grams", value.saturatedFat) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("selenium_grams", value.selenium) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("sodium_grams", value.sodium) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("sugar_grams", value.sugar) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("thiamin_grams", value.thiamin) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("total_carbohydrate_grams", value.totalCarbohydrate) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("total_fat_grams", value.totalFat) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("trans_fat_grams", value.transFat) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("unsaturated_fat_grams", value.unsaturatedFat) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("vitamin_a_grams", value.vitaminA) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("vitamin_b12_grams", value.vitaminB12) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("vitamin_b6_grams", value.vitaminB6) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("vitamin_c_grams", value.vitaminC) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("vitamin_d_grams", value.vitaminD) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("vitamin_e_grams", value.vitaminE) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("vitamin_k_grams", value.vitaminK) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("zinc_grams", value.zinc) { item, fieldPath -> number(item.inGrams, fieldPath) }
        field("name", value.name) { item, fieldPath -> text(item, fieldPath) }
        field("meal_type", value.mealType) { item, fieldPath -> constant(item, MealType, fieldPath) }
    }

    private fun fields(value: OvulationTestRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("result", value.result) { item, fieldPath -> constant(item, OvulationTestResult, fieldPath) }
    }

    private fun fields(value: OxygenSaturationRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("percentage", value.percentage) { item, fieldPath -> number(item.value, fieldPath) }
    }

    private fun fields(value: PlannedExerciseSessionRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("has_explicit_time", value.hasExplicitTime) { item, fieldPath -> if (item) "true" else "false" }
        field("exercise_type", value.exerciseType) { item, fieldPath -> constant(item, ExerciseType, fieldPath) }
        field("title", value.title) { item, fieldPath -> text(item, fieldPath) }
        field("notes", value.notes) { item, fieldPath -> text(item, fieldPath) }
        field("completed_exercise_session_id", value.completedExerciseSessionId) { item, fieldPath -> text(item, fieldPath) }
        field("blocks", value.blocks) { item, fieldPath -> array(item, fieldPath, ::fields) }
    }

    private fun fields(value: PowerRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("samples", value.samples) { item, fieldPath -> array(item, fieldPath, ::fields) }
    }

    private fun fields(value: RespiratoryRateRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("rate", value.rate) { item, fieldPath -> number(item, fieldPath) }
    }

    private fun fields(value: RestingHeartRateRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("beats_per_minute", value.beatsPerMinute) { item, fieldPath -> item.toString() }
    }

    private fun fields(value: SexualActivityRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("protection_used", value.protectionUsed) { item, fieldPath -> constant(item, SexualActivityProtectionUsed, fieldPath) }
    }

    private fun fields(value: SkinTemperatureRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("baseline_celsius", value.baseline) { item, fieldPath -> number(item.inCelsius, fieldPath) }
        field("deltas", value.deltas) { item, fieldPath -> array(item, fieldPath, ::fields) }
        field("measurement_location", value.measurementLocation) { item, fieldPath -> constant(item, SkinTemperatureMeasurementLocation, fieldPath) }
    }

    private fun fields(value: SleepSessionRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("title", value.title) { item, fieldPath -> text(item, fieldPath) }
        field("notes", value.notes) { item, fieldPath -> text(item, fieldPath) }
        field("stages", value.stages) { item, fieldPath -> array(item, fieldPath, ::fields) }
    }

    private fun fields(value: SpeedRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("samples", value.samples) { item, fieldPath -> array(item, fieldPath, ::fields) }
    }

    private fun fields(value: StepsRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("count", value.count) { item, fieldPath -> item.toString() }
    }

    private fun fields(value: StepsCadenceRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("samples", value.samples) { item, fieldPath -> array(item, fieldPath, ::fields) }
    }

    private fun fields(value: TotalCaloriesBurnedRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("energy_calories", value.energy) { item, fieldPath -> number(item.inCalories, fieldPath) }
    }

    private fun fields(value: WeightRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("weight_grams", value.weight) { item, fieldPath -> number(item.inGrams, fieldPath) }
    }

    private fun fields(value: Vo2MaxRecord, path: String): String = jsonObject(path) {
        field("time", value.time) { item, fieldPath -> instant(item, fieldPath) }
        field("zone_offset", value.zoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("vo2_milliliters_per_minute_kilogram", value.vo2MillilitersPerMinuteKilogram) { item, fieldPath -> number(item, fieldPath) }
        field("measurement_method", value.measurementMethod) { item, fieldPath -> constant(item, Vo2MaxMeasurementMethod, fieldPath) }
    }

    private fun fields(value: WheelchairPushesRecord, path: String): String = jsonObject(path) {
        field("start_time", value.startTime) { item, fieldPath -> instant(item, fieldPath) }
        field("start_zone_offset", value.startZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("end_time", value.endTime) { item, fieldPath -> instant(item, fieldPath) }
        field("end_zone_offset", value.endZoneOffset) { item, fieldPath -> offset(item, fieldPath) }
        field("count", value.count) { item, fieldPath -> item.toString() }
    }

    private fun fields(value: ExerciseRouteResult, path: String): String = when (value) {
        is ExerciseRouteResult.Data -> jsonObject(path) {
            field("kind", "data", ::text)
            field("exercise_route", value.exerciseRoute) { item, fieldPath -> fields(item, fieldPath) }
        }
        is ExerciseRouteResult.NoData -> jsonObject(path) {
            field("kind", "no-data", ::text)
        }
        is ExerciseRouteResult.ConsentRequired -> jsonObject(path) {
            field("kind", "consent-required", ::text)
        }
        else -> throw ProjectionFailure("undeclared-variant", path)
    }

    private fun fields(value: ExerciseCompletionGoal, path: String): String = when (value) {
        is ExerciseCompletionGoal.ActiveCaloriesBurnedGoal -> jsonObject(path) {
            field("kind", "active-calories-burned", ::text)
            field("active_calories_calories", value.activeCalories) { item, fieldPath -> number(item.inCalories, fieldPath) }
        }
        is ExerciseCompletionGoal.DistanceAndDurationGoal -> jsonObject(path) {
            field("kind", "distance-and-duration", ::text)
            field("distance_meters", value.distance) { item, fieldPath -> number(item.inMeters, fieldPath) }
            field("duration", value.duration) { item, fieldPath -> duration(item) }
        }
        is ExerciseCompletionGoal.DistanceGoal -> jsonObject(path) {
            field("kind", "distance", ::text)
            field("distance_meters", value.distance) { item, fieldPath -> number(item.inMeters, fieldPath) }
        }
        is ExerciseCompletionGoal.DurationGoal -> jsonObject(path) {
            field("kind", "duration", ::text)
            field("duration", value.duration) { item, fieldPath -> duration(item) }
        }
        is ExerciseCompletionGoal.ManualCompletion -> jsonObject(path) {
            field("kind", "manual-completion", ::text)
        }
        is ExerciseCompletionGoal.RepetitionsGoal -> jsonObject(path) {
            field("kind", "repetitions", ::text)
            field("repetitions", value.repetitions) { item, fieldPath -> item.toString() }
        }
        is ExerciseCompletionGoal.StepsGoal -> jsonObject(path) {
            field("kind", "steps", ::text)
            field("steps", value.steps) { item, fieldPath -> item.toString() }
        }
        is ExerciseCompletionGoal.TotalCaloriesBurnedGoal -> jsonObject(path) {
            field("kind", "total-calories-burned", ::text)
            field("total_calories_calories", value.totalCalories) { item, fieldPath -> number(item.inCalories, fieldPath) }
        }
        else -> throw ProjectionFailure("undeclared-variant", path)
    }

    private fun fields(value: ExercisePerformanceTarget, path: String): String = when (value) {
        is ExercisePerformanceTarget.AmrapTarget -> jsonObject(path) {
            field("kind", "amrap", ::text)
        }
        is ExercisePerformanceTarget.CadenceTarget -> jsonObject(path) {
            field("kind", "cadence", ::text)
            field("min_cadence", value.minCadence) { item, fieldPath -> number(item, fieldPath) }
            field("max_cadence", value.maxCadence) { item, fieldPath -> number(item, fieldPath) }
        }
        is ExercisePerformanceTarget.HeartRateTarget -> jsonObject(path) {
            field("kind", "heart-rate", ::text)
            field("min_heart_rate", value.minHeartRate) { item, fieldPath -> number(item, fieldPath) }
            field("max_heart_rate", value.maxHeartRate) { item, fieldPath -> number(item, fieldPath) }
        }
        is ExercisePerformanceTarget.PowerTarget -> jsonObject(path) {
            field("kind", "power", ::text)
            field("min_power_watts", value.minPower) { item, fieldPath -> number(item.inWatts, fieldPath) }
            field("max_power_watts", value.maxPower) { item, fieldPath -> number(item.inWatts, fieldPath) }
        }
        is ExercisePerformanceTarget.RateOfPerceivedExertionTarget -> jsonObject(path) {
            field("kind", "rate-of-perceived-exertion", ::text)
            field("rpe", value.rpe) { item, fieldPath -> item.toString() }
        }
        is ExercisePerformanceTarget.SpeedTarget -> jsonObject(path) {
            field("kind", "speed", ::text)
            field("min_speed_meters_per_second", value.minSpeed) { item, fieldPath -> number(item.inMetersPerSecond, fieldPath) }
            field("max_speed_meters_per_second", value.maxSpeed) { item, fieldPath -> number(item.inMetersPerSecond, fieldPath) }
        }
        is ExercisePerformanceTarget.WeightTarget -> jsonObject(path) {
            field("kind", "weight", ::text)
            field("mass_grams", value.mass) { item, fieldPath -> number(item.inGrams, fieldPath) }
        }
        else -> throw ProjectionFailure("undeclared-variant", path)
    }

    private fun constant(value: Int, allowed: Set<Int>, path: String): String {
        if (value !in allowed) throw ProjectionFailure("undeclared-constant", path)
        return value.toString()
    }

    // Explicit sets pin the model, including gaps in the SDK exercise-type constants.
    private val RecordingMethod = setOf(
        0, 1, 2, 3,
    )
    private val DeviceType = setOf(
        0, 1, 2, 3, 4, 5, 6, 7, 8,
    )
    private val MealType = setOf(
        0, 1, 2, 3, 4,
    )
    private val BloodGlucoseSpecimenSource = setOf(
        0, 1, 2, 3, 4, 5, 6,
    )
    private val BloodGlucoseRelationToMeal = setOf(
        0, 1, 2, 3, 4,
    )
    private val BloodPressureBodyPosition = setOf(
        0, 1, 2, 3, 4,
    )
    private val BloodPressureMeasurementLocation = setOf(
        0, 1, 2, 3, 4,
    )
    private val BodyTemperatureMeasurementLocation = setOf(
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    )
    private val CervicalMucusAppearance = setOf(
        0, 1, 2, 3, 4, 5, 6,
    )
    private val CervicalMucusSensation = setOf(
        0, 1, 2, 3,
    )
    private val ExerciseType = setOf(
        0, 2, 4, 5, 8, 9, 10, 11, 13, 14, 16, 25, 26, 27, 28, 29,
        31, 32, 33, 34, 35, 36, 37, 38, 39, 44, 46, 47, 48, 50, 51, 52,
        53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 68, 69,
        70, 71, 72, 73, 74, 75, 76, 78, 79, 80, 81, 82, 83,
    )
    private val ExerciseSegmentType = setOf(
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
        16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
        32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47,
        48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63,
        64, 65, 66, 67,
    )
    private val ExercisePhase = setOf(
        0, 1, 2, 3, 4, 5,
    )
    private val MenstruationFlow = setOf(
        0, 1, 2, 3,
    )
    private val MindfulnessSessionType = setOf(
        0, 1, 2, 3, 4, 5,
    )
    private val OvulationTestResult = setOf(
        0, 1, 2, 3,
    )
    private val SexualActivityProtectionUsed = setOf(
        0, 1, 2,
    )
    private val SkinTemperatureMeasurementLocation = setOf(
        0, 1, 2, 3,
    )
    private val Vo2MaxMeasurementMethod = setOf(
        0, 1, 2, 3, 4, 5,
    )
    private val SleepStage = setOf(
        0, 1, 2, 3, 4, 5, 6, 7,
    )
}
