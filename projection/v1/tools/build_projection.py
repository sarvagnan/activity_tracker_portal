"""Generate the project-source-record@1 projection model, schemas, and vectors.

The model below is the pinned SDK inventory for androidx.health.connect:
connect-client 1.1.0, extracted from the library's bytecode: every public
property of every health-and-fitness record class and nested type, its
nullability, the SDK constants each integer field declares, and the storage
unit Health Connect persists for each unit class. Run with --check to verify
that the checked-in files under projection/v1 match this generator exactly.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK_VERSION = "androidx.health.connect:connect-client:1.1.0"
SCHEMA_VERSION = 1

# Health Connect persists each unit class in one unit; the client's platform
# converters construct SDK values in that unit on read (UnitConvertersKt).
UNITS: dict[str, str] = {
    "BloodGlucose": "millimoles_per_liter",
    "Energy": "calories",
    "Length": "meters",
    "Mass": "grams",
    "Power": "watts",
    "Pressure": "millimeters_of_mercury",
    "Temperature": "celsius",
    "TemperatureDelta": "celsius",
    "Velocity": "meters_per_second",
    "Volume": "liters",
}
CONSTANTS: dict[str, tuple[str, dict[str, int]]] = {
    "recording-method": ("Metadata", {
        "RECORDING_METHOD_UNKNOWN": 0,
        "RECORDING_METHOD_ACTIVELY_RECORDED": 1,
        "RECORDING_METHOD_AUTOMATICALLY_RECORDED": 2,
        "RECORDING_METHOD_MANUAL_ENTRY": 3,
    }),
    "device-type": ("Device", {
        "TYPE_UNKNOWN": 0,
        "TYPE_WATCH": 1,
        "TYPE_PHONE": 2,
        "TYPE_SCALE": 3,
        "TYPE_RING": 4,
        "TYPE_HEAD_MOUNTED": 5,
        "TYPE_FITNESS_BAND": 6,
        "TYPE_CHEST_STRAP": 7,
        "TYPE_SMART_DISPLAY": 8,
    }),
    "meal-type": ("MealType", {
        "MEAL_TYPE_UNKNOWN": 0,
        "MEAL_TYPE_BREAKFAST": 1,
        "MEAL_TYPE_LUNCH": 2,
        "MEAL_TYPE_DINNER": 3,
        "MEAL_TYPE_SNACK": 4,
    }),
    "blood-glucose-specimen-source": ("BloodGlucoseRecord", {
        "SPECIMEN_SOURCE_UNKNOWN": 0,
        "SPECIMEN_SOURCE_INTERSTITIAL_FLUID": 1,
        "SPECIMEN_SOURCE_CAPILLARY_BLOOD": 2,
        "SPECIMEN_SOURCE_PLASMA": 3,
        "SPECIMEN_SOURCE_SERUM": 4,
        "SPECIMEN_SOURCE_TEARS": 5,
        "SPECIMEN_SOURCE_WHOLE_BLOOD": 6,
    }),
    "blood-glucose-relation-to-meal": ("BloodGlucoseRecord", {
        "RELATION_TO_MEAL_UNKNOWN": 0,
        "RELATION_TO_MEAL_GENERAL": 1,
        "RELATION_TO_MEAL_FASTING": 2,
        "RELATION_TO_MEAL_BEFORE_MEAL": 3,
        "RELATION_TO_MEAL_AFTER_MEAL": 4,
    }),
    "blood-pressure-body-position": ("BloodPressureRecord", {
        "BODY_POSITION_UNKNOWN": 0,
        "BODY_POSITION_STANDING_UP": 1,
        "BODY_POSITION_SITTING_DOWN": 2,
        "BODY_POSITION_LYING_DOWN": 3,
        "BODY_POSITION_RECLINING": 4,
    }),
    "blood-pressure-measurement-location": ("BloodPressureRecord", {
        "MEASUREMENT_LOCATION_UNKNOWN": 0,
        "MEASUREMENT_LOCATION_LEFT_WRIST": 1,
        "MEASUREMENT_LOCATION_RIGHT_WRIST": 2,
        "MEASUREMENT_LOCATION_LEFT_UPPER_ARM": 3,
        "MEASUREMENT_LOCATION_RIGHT_UPPER_ARM": 4,
    }),
    "body-temperature-measurement-location": ("BodyTemperatureMeasurementLocation", {
        "MEASUREMENT_LOCATION_UNKNOWN": 0,
        "MEASUREMENT_LOCATION_ARMPIT": 1,
        "MEASUREMENT_LOCATION_FINGER": 2,
        "MEASUREMENT_LOCATION_FOREHEAD": 3,
        "MEASUREMENT_LOCATION_MOUTH": 4,
        "MEASUREMENT_LOCATION_RECTUM": 5,
        "MEASUREMENT_LOCATION_TEMPORAL_ARTERY": 6,
        "MEASUREMENT_LOCATION_TOE": 7,
        "MEASUREMENT_LOCATION_EAR": 8,
        "MEASUREMENT_LOCATION_WRIST": 9,
        "MEASUREMENT_LOCATION_VAGINA": 10,
    }),
    "cervical-mucus-appearance": ("CervicalMucusRecord", {
        "APPEARANCE_UNKNOWN": 0,
        "APPEARANCE_DRY": 1,
        "APPEARANCE_STICKY": 2,
        "APPEARANCE_CREAMY": 3,
        "APPEARANCE_WATERY": 4,
        "APPEARANCE_EGG_WHITE": 5,
        "APPEARANCE_UNUSUAL": 6,
    }),
    "cervical-mucus-sensation": ("CervicalMucusRecord", {
        "SENSATION_UNKNOWN": 0,
        "SENSATION_LIGHT": 1,
        "SENSATION_MEDIUM": 2,
        "SENSATION_HEAVY": 3,
    }),
    "exercise-type": ("ExerciseSessionRecord", {
        "EXERCISE_TYPE_OTHER_WORKOUT": 0,
        "EXERCISE_TYPE_BADMINTON": 2,
        "EXERCISE_TYPE_BASEBALL": 4,
        "EXERCISE_TYPE_BASKETBALL": 5,
        "EXERCISE_TYPE_BIKING": 8,
        "EXERCISE_TYPE_BIKING_STATIONARY": 9,
        "EXERCISE_TYPE_BOOT_CAMP": 10,
        "EXERCISE_TYPE_BOXING": 11,
        "EXERCISE_TYPE_CALISTHENICS": 13,
        "EXERCISE_TYPE_CRICKET": 14,
        "EXERCISE_TYPE_DANCING": 16,
        "EXERCISE_TYPE_ELLIPTICAL": 25,
        "EXERCISE_TYPE_EXERCISE_CLASS": 26,
        "EXERCISE_TYPE_FENCING": 27,
        "EXERCISE_TYPE_FOOTBALL_AMERICAN": 28,
        "EXERCISE_TYPE_FOOTBALL_AUSTRALIAN": 29,
        "EXERCISE_TYPE_FRISBEE_DISC": 31,
        "EXERCISE_TYPE_GOLF": 32,
        "EXERCISE_TYPE_GUIDED_BREATHING": 33,
        "EXERCISE_TYPE_GYMNASTICS": 34,
        "EXERCISE_TYPE_HANDBALL": 35,
        "EXERCISE_TYPE_HIGH_INTENSITY_INTERVAL_TRAINING": 36,
        "EXERCISE_TYPE_HIKING": 37,
        "EXERCISE_TYPE_ICE_HOCKEY": 38,
        "EXERCISE_TYPE_ICE_SKATING": 39,
        "EXERCISE_TYPE_MARTIAL_ARTS": 44,
        "EXERCISE_TYPE_PADDLING": 46,
        "EXERCISE_TYPE_PARAGLIDING": 47,
        "EXERCISE_TYPE_PILATES": 48,
        "EXERCISE_TYPE_RACQUETBALL": 50,
        "EXERCISE_TYPE_ROCK_CLIMBING": 51,
        "EXERCISE_TYPE_ROLLER_HOCKEY": 52,
        "EXERCISE_TYPE_ROWING": 53,
        "EXERCISE_TYPE_ROWING_MACHINE": 54,
        "EXERCISE_TYPE_RUGBY": 55,
        "EXERCISE_TYPE_RUNNING": 56,
        "EXERCISE_TYPE_RUNNING_TREADMILL": 57,
        "EXERCISE_TYPE_SAILING": 58,
        "EXERCISE_TYPE_SCUBA_DIVING": 59,
        "EXERCISE_TYPE_SKATING": 60,
        "EXERCISE_TYPE_SKIING": 61,
        "EXERCISE_TYPE_SNOWBOARDING": 62,
        "EXERCISE_TYPE_SNOWSHOEING": 63,
        "EXERCISE_TYPE_SOCCER": 64,
        "EXERCISE_TYPE_SOFTBALL": 65,
        "EXERCISE_TYPE_SQUASH": 66,
        "EXERCISE_TYPE_STAIR_CLIMBING": 68,
        "EXERCISE_TYPE_STAIR_CLIMBING_MACHINE": 69,
        "EXERCISE_TYPE_STRENGTH_TRAINING": 70,
        "EXERCISE_TYPE_STRETCHING": 71,
        "EXERCISE_TYPE_SURFING": 72,
        "EXERCISE_TYPE_SWIMMING_OPEN_WATER": 73,
        "EXERCISE_TYPE_SWIMMING_POOL": 74,
        "EXERCISE_TYPE_TABLE_TENNIS": 75,
        "EXERCISE_TYPE_TENNIS": 76,
        "EXERCISE_TYPE_VOLLEYBALL": 78,
        "EXERCISE_TYPE_WALKING": 79,
        "EXERCISE_TYPE_WATER_POLO": 80,
        "EXERCISE_TYPE_WEIGHTLIFTING": 81,
        "EXERCISE_TYPE_WHEELCHAIR": 82,
        "EXERCISE_TYPE_YOGA": 83,
    }),
    "exercise-segment-type": ("ExerciseSegment", {
        "EXERCISE_SEGMENT_TYPE_UNKNOWN": 0,
        "EXERCISE_SEGMENT_TYPE_ARM_CURL": 1,
        "EXERCISE_SEGMENT_TYPE_BACK_EXTENSION": 2,
        "EXERCISE_SEGMENT_TYPE_BALL_SLAM": 3,
        "EXERCISE_SEGMENT_TYPE_BARBELL_SHOULDER_PRESS": 4,
        "EXERCISE_SEGMENT_TYPE_BENCH_PRESS": 5,
        "EXERCISE_SEGMENT_TYPE_BENCH_SIT_UP": 6,
        "EXERCISE_SEGMENT_TYPE_BIKING": 7,
        "EXERCISE_SEGMENT_TYPE_BIKING_STATIONARY": 8,
        "EXERCISE_SEGMENT_TYPE_BURPEE": 9,
        "EXERCISE_SEGMENT_TYPE_CRUNCH": 10,
        "EXERCISE_SEGMENT_TYPE_DEADLIFT": 11,
        "EXERCISE_SEGMENT_TYPE_DOUBLE_ARM_TRICEPS_EXTENSION": 12,
        "EXERCISE_SEGMENT_TYPE_DUMBBELL_CURL_LEFT_ARM": 13,
        "EXERCISE_SEGMENT_TYPE_DUMBBELL_CURL_RIGHT_ARM": 14,
        "EXERCISE_SEGMENT_TYPE_DUMBBELL_FRONT_RAISE": 15,
        "EXERCISE_SEGMENT_TYPE_DUMBBELL_LATERAL_RAISE": 16,
        "EXERCISE_SEGMENT_TYPE_DUMBBELL_ROW": 17,
        "EXERCISE_SEGMENT_TYPE_DUMBBELL_TRICEPS_EXTENSION_LEFT_ARM": 18,
        "EXERCISE_SEGMENT_TYPE_DUMBBELL_TRICEPS_EXTENSION_RIGHT_ARM": 19,
        "EXERCISE_SEGMENT_TYPE_DUMBBELL_TRICEPS_EXTENSION_TWO_ARM": 20,
        "EXERCISE_SEGMENT_TYPE_ELLIPTICAL": 21,
        "EXERCISE_SEGMENT_TYPE_FORWARD_TWIST": 22,
        "EXERCISE_SEGMENT_TYPE_FRONT_RAISE": 23,
        "EXERCISE_SEGMENT_TYPE_HIGH_INTENSITY_INTERVAL_TRAINING": 24,
        "EXERCISE_SEGMENT_TYPE_HIP_THRUST": 25,
        "EXERCISE_SEGMENT_TYPE_HULA_HOOP": 26,
        "EXERCISE_SEGMENT_TYPE_JUMPING_JACK": 27,
        "EXERCISE_SEGMENT_TYPE_JUMP_ROPE": 28,
        "EXERCISE_SEGMENT_TYPE_KETTLEBELL_SWING": 29,
        "EXERCISE_SEGMENT_TYPE_LATERAL_RAISE": 30,
        "EXERCISE_SEGMENT_TYPE_LAT_PULL_DOWN": 31,
        "EXERCISE_SEGMENT_TYPE_LEG_CURL": 32,
        "EXERCISE_SEGMENT_TYPE_LEG_EXTENSION": 33,
        "EXERCISE_SEGMENT_TYPE_LEG_PRESS": 34,
        "EXERCISE_SEGMENT_TYPE_LEG_RAISE": 35,
        "EXERCISE_SEGMENT_TYPE_LUNGE": 36,
        "EXERCISE_SEGMENT_TYPE_MOUNTAIN_CLIMBER": 37,
        "EXERCISE_SEGMENT_TYPE_OTHER_WORKOUT": 38,
        "EXERCISE_SEGMENT_TYPE_PAUSE": 39,
        "EXERCISE_SEGMENT_TYPE_PILATES": 40,
        "EXERCISE_SEGMENT_TYPE_PLANK": 41,
        "EXERCISE_SEGMENT_TYPE_PULL_UP": 42,
        "EXERCISE_SEGMENT_TYPE_PUNCH": 43,
        "EXERCISE_SEGMENT_TYPE_REST": 44,
        "EXERCISE_SEGMENT_TYPE_ROWING_MACHINE": 45,
        "EXERCISE_SEGMENT_TYPE_RUNNING": 46,
        "EXERCISE_SEGMENT_TYPE_RUNNING_TREADMILL": 47,
        "EXERCISE_SEGMENT_TYPE_SHOULDER_PRESS": 48,
        "EXERCISE_SEGMENT_TYPE_SINGLE_ARM_TRICEPS_EXTENSION": 49,
        "EXERCISE_SEGMENT_TYPE_SIT_UP": 50,
        "EXERCISE_SEGMENT_TYPE_SQUAT": 51,
        "EXERCISE_SEGMENT_TYPE_STAIR_CLIMBING": 52,
        "EXERCISE_SEGMENT_TYPE_STAIR_CLIMBING_MACHINE": 53,
        "EXERCISE_SEGMENT_TYPE_STRETCHING": 54,
        "EXERCISE_SEGMENT_TYPE_SWIMMING_BACKSTROKE": 55,
        "EXERCISE_SEGMENT_TYPE_SWIMMING_BREASTSTROKE": 56,
        "EXERCISE_SEGMENT_TYPE_SWIMMING_BUTTERFLY": 57,
        "EXERCISE_SEGMENT_TYPE_SWIMMING_FREESTYLE": 58,
        "EXERCISE_SEGMENT_TYPE_SWIMMING_MIXED": 59,
        "EXERCISE_SEGMENT_TYPE_SWIMMING_OPEN_WATER": 60,
        "EXERCISE_SEGMENT_TYPE_SWIMMING_OTHER": 61,
        "EXERCISE_SEGMENT_TYPE_SWIMMING_POOL": 62,
        "EXERCISE_SEGMENT_TYPE_UPPER_TWIST": 63,
        "EXERCISE_SEGMENT_TYPE_WALKING": 64,
        "EXERCISE_SEGMENT_TYPE_WEIGHTLIFTING": 65,
        "EXERCISE_SEGMENT_TYPE_WHEELCHAIR": 66,
        "EXERCISE_SEGMENT_TYPE_YOGA": 67,
    }),
    "exercise-phase": ("PlannedExerciseStep", {
        "EXERCISE_PHASE_UNKNOWN": 0,
        "EXERCISE_PHASE_WARMUP": 1,
        "EXERCISE_PHASE_REST": 2,
        "EXERCISE_PHASE_ACTIVE": 3,
        "EXERCISE_PHASE_COOLDOWN": 4,
        "EXERCISE_PHASE_RECOVERY": 5,
    }),
    "menstruation-flow": ("MenstruationFlowRecord", {
        "FLOW_UNKNOWN": 0,
        "FLOW_LIGHT": 1,
        "FLOW_MEDIUM": 2,
        "FLOW_HEAVY": 3,
    }),
    "mindfulness-session-type": ("MindfulnessSessionRecord", {
        "MINDFULNESS_SESSION_TYPE_UNKNOWN": 0,
        "MINDFULNESS_SESSION_TYPE_MEDITATION": 1,
        "MINDFULNESS_SESSION_TYPE_BREATHING": 2,
        "MINDFULNESS_SESSION_TYPE_MUSIC": 3,
        "MINDFULNESS_SESSION_TYPE_MOVEMENT": 4,
        "MINDFULNESS_SESSION_TYPE_UNGUIDED": 5,
    }),
    "ovulation-test-result": ("OvulationTestRecord", {
        "RESULT_INCONCLUSIVE": 0,
        "RESULT_POSITIVE": 1,
        "RESULT_HIGH": 2,
        "RESULT_NEGATIVE": 3,
    }),
    "sexual-activity-protection-used": ("SexualActivityRecord", {
        "PROTECTION_USED_UNKNOWN": 0,
        "PROTECTION_USED_PROTECTED": 1,
        "PROTECTION_USED_UNPROTECTED": 2,
    }),
    "skin-temperature-measurement-location": ("SkinTemperatureRecord", {
        "MEASUREMENT_LOCATION_UNKNOWN": 0,
        "MEASUREMENT_LOCATION_FINGER": 1,
        "MEASUREMENT_LOCATION_TOE": 2,
        "MEASUREMENT_LOCATION_WRIST": 3,
    }),
    "vo2-max-measurement-method": ("Vo2MaxRecord", {
        "MEASUREMENT_METHOD_OTHER": 0,
        "MEASUREMENT_METHOD_METABOLIC_CART": 1,
        "MEASUREMENT_METHOD_HEART_RATE_RATIO": 2,
        "MEASUREMENT_METHOD_COOPER_TEST": 3,
        "MEASUREMENT_METHOD_MULTISTAGE_FITNESS_TEST": 4,
        "MEASUREMENT_METHOD_ROCKPORT_FITNESS_TEST": 5,
    }),
    "sleep-stage": ("SleepSessionRecord", {
        "STAGE_TYPE_UNKNOWN": 0,
        "STAGE_TYPE_AWAKE": 1,
        "STAGE_TYPE_SLEEPING": 2,
        "STAGE_TYPE_OUT_OF_BED": 3,
        "STAGE_TYPE_LIGHT": 4,
        "STAGE_TYPE_DEEP": 5,
        "STAGE_TYPE_REM": 6,
        "STAGE_TYPE_AWAKE_IN_BED": 7,
    }),
}

# Field kinds: instant, offset, duration, string, bool, int, long, long-string,
# double, percentage, measure:<Unit>, constant:<group>, object:<Type>,
# list:<Type>, variant:<Sealed>, list-variant:<Sealed>. A trailing "?" marks a
# nullable property, which is omitted from the projection when absent.
TYPES: dict[str, list[tuple[str, str]]] = {
    "metadata.Metadata": [
        ("id", "string"),
        ("dataOrigin", "object:metadata.DataOrigin"),
        ("lastModifiedTime", "instant"),
        ("clientRecordId", "string?"),
        ("clientRecordVersion", "long-string"),
        ("recordingMethod", "constant:recording-method"),
        ("device", "object:metadata.Device?"),
    ],
    "metadata.DataOrigin": [("packageName", "string")],
    "metadata.Device": [
        ("type", "constant:device-type"),
        ("manufacturer", "string?"),
        ("model", "string?"),
    ],
    "CyclingPedalingCadenceRecord$Sample": [("time", "instant"), ("revolutionsPerMinute", "double")],
    "HeartRateRecord$Sample": [("time", "instant"), ("beatsPerMinute", "long")],
    "PowerRecord$Sample": [("time", "instant"), ("power", "measure:Power")],
    "SpeedRecord$Sample": [("time", "instant"), ("speed", "measure:Velocity")],
    "StepsCadenceRecord$Sample": [("time", "instant"), ("rate", "double")],
    "SkinTemperatureRecord$Delta": [("time", "instant"), ("delta", "measure:TemperatureDelta")],
    "SleepSessionRecord$Stage": [("startTime", "instant"), ("endTime", "instant"), ("stage", "constant:sleep-stage")],
    "ExerciseSegment": [
        ("startTime", "instant"),
        ("endTime", "instant"),
        ("segmentType", "constant:exercise-segment-type"),
        ("repetitions", "int"),
    ],
    "ExerciseLap": [("startTime", "instant"), ("endTime", "instant"), ("length", "measure:Length?")],
    "ExerciseRoute": [("route", "list:ExerciseRoute$Location")],
    "ExerciseRoute$Location": [
        ("time", "instant"),
        ("latitude", "double"),
        ("longitude", "double"),
        ("horizontalAccuracy", "measure:Length?"),
        ("verticalAccuracy", "measure:Length?"),
        ("altitude", "measure:Length?"),
    ],
    "PlannedExerciseBlock": [
        ("repetitions", "int"),
        ("steps", "list:PlannedExerciseStep"),
        ("description", "string?"),
    ],
    "PlannedExerciseStep": [
        ("exerciseType", "constant:exercise-segment-type"),
        ("exercisePhase", "constant:exercise-phase"),
        ("completionGoal", "variant:ExerciseCompletionGoal"),
        ("performanceTargets", "list-variant:ExercisePerformanceTarget"),
        ("description", "string?"),
    ],
}

# Sealed hierarchies: kind -> (SDK class, fields). SDK Unknown* variants are
# deliberately absent, so projecting one is undeclared-variant.
VARIANTS: dict[str, dict[str, tuple[str, list[tuple[str, str]]]]] = {
    "ExerciseRouteResult": {
        "data": ("ExerciseRouteResult$Data", [("exerciseRoute", "object:ExerciseRoute")]),
        "no-data": ("ExerciseRouteResult$NoData", []),
        "consent-required": ("ExerciseRouteResult$ConsentRequired", []),
    },
    "ExerciseCompletionGoal": {
        "active-calories-burned": ("ExerciseCompletionGoal$ActiveCaloriesBurnedGoal", [("activeCalories", "measure:Energy")]),
        "distance-and-duration": ("ExerciseCompletionGoal$DistanceAndDurationGoal", [("distance", "measure:Length"), ("duration", "duration")]),
        "distance": ("ExerciseCompletionGoal$DistanceGoal", [("distance", "measure:Length")]),
        "duration": ("ExerciseCompletionGoal$DurationGoal", [("duration", "duration")]),
        "manual-completion": ("ExerciseCompletionGoal$ManualCompletion", []),
        "repetitions": ("ExerciseCompletionGoal$RepetitionsGoal", [("repetitions", "int")]),
        "steps": ("ExerciseCompletionGoal$StepsGoal", [("steps", "int")]),
        "total-calories-burned": ("ExerciseCompletionGoal$TotalCaloriesBurnedGoal", [("totalCalories", "measure:Energy")]),
    },
    "ExercisePerformanceTarget": {
        "amrap": ("ExercisePerformanceTarget$AmrapTarget", []),
        "cadence": ("ExercisePerformanceTarget$CadenceTarget", [("minCadence", "double"), ("maxCadence", "double")]),
        "heart-rate": ("ExercisePerformanceTarget$HeartRateTarget", [("minHeartRate", "double"), ("maxHeartRate", "double")]),
        "power": ("ExercisePerformanceTarget$PowerTarget", [("minPower", "measure:Power"), ("maxPower", "measure:Power")]),
        "rate-of-perceived-exertion": ("ExercisePerformanceTarget$RateOfPerceivedExertionTarget", [("rpe", "int")]),
        "speed": ("ExercisePerformanceTarget$SpeedTarget", [("minSpeed", "measure:Velocity"), ("maxSpeed", "measure:Velocity")]),
        "weight": ("ExercisePerformanceTarget$WeightTarget", [("mass", "measure:Mass")]),
    },
}

INSTANT_FIELDS = [("time", "instant"), ("zoneOffset", "offset?")]
INTERVAL_FIELDS = [("startTime", "instant"), ("startZoneOffset", "offset?"), ("endTime", "instant"), ("endZoneOffset", "offset?")]
NUTRIENTS = [
    "biotin", "caffeine", "calcium", "chloride", "cholesterol", "chromium", "copper", "dietaryFiber", "folate",
    "folicAcid", "iodine", "iron", "magnesium", "manganese", "molybdenum", "monounsaturatedFat", "niacin",
    "pantothenicAcid", "phosphorus", "polyunsaturatedFat", "potassium", "protein", "riboflavin", "saturatedFat",
    "selenium", "sodium", "sugar", "thiamin", "totalCarbohydrate", "totalFat", "transFat", "unsaturatedFat",
    "vitaminA", "vitaminB12", "vitaminB6", "vitaminC", "vitaminD", "vitaminE", "vitaminK", "zinc",
]

# record type -> (SDK class, time shape, record-specific fields)
RECORDS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "active-calories-burned": ("ActiveCaloriesBurnedRecord", "interval", [("energy", "measure:Energy")]),
    "basal-body-temperature": ("BasalBodyTemperatureRecord", "instantaneous", [("temperature", "measure:Temperature"), ("measurementLocation", "constant:body-temperature-measurement-location")]),
    "basal-metabolic-rate": ("BasalMetabolicRateRecord", "instantaneous", [("basalMetabolicRate", "measure:Power")]),
    "blood-glucose": ("BloodGlucoseRecord", "instantaneous", [("level", "measure:BloodGlucose"), ("specimenSource", "constant:blood-glucose-specimen-source"), ("mealType", "constant:meal-type"), ("relationToMeal", "constant:blood-glucose-relation-to-meal")]),
    "blood-pressure": ("BloodPressureRecord", "instantaneous", [("systolic", "measure:Pressure"), ("diastolic", "measure:Pressure"), ("bodyPosition", "constant:blood-pressure-body-position"), ("measurementLocation", "constant:blood-pressure-measurement-location")]),
    "body-fat": ("BodyFatRecord", "instantaneous", [("percentage", "percentage")]),
    "body-temperature": ("BodyTemperatureRecord", "instantaneous", [("temperature", "measure:Temperature"), ("measurementLocation", "constant:body-temperature-measurement-location")]),
    "body-water-mass": ("BodyWaterMassRecord", "instantaneous", [("mass", "measure:Mass")]),
    "bone-mass": ("BoneMassRecord", "instantaneous", [("mass", "measure:Mass")]),
    "cervical-mucus": ("CervicalMucusRecord", "instantaneous", [("appearance", "constant:cervical-mucus-appearance"), ("sensation", "constant:cervical-mucus-sensation")]),
    "cycling-pedaling-cadence": ("CyclingPedalingCadenceRecord", "series", [("samples", "list:CyclingPedalingCadenceRecord$Sample")]),
    "distance": ("DistanceRecord", "interval", [("distance", "measure:Length")]),
    "elevation-gained": ("ElevationGainedRecord", "interval", [("elevation", "measure:Length")]),
    "exercise-session": ("ExerciseSessionRecord", "interval", [("exerciseType", "constant:exercise-type"), ("title", "string?"), ("notes", "string?"), ("segments", "list:ExerciseSegment"), ("laps", "list:ExerciseLap"), ("exerciseRouteResult", "variant:ExerciseRouteResult"), ("plannedExerciseSessionId", "string?")]),
    "floors-climbed": ("FloorsClimbedRecord", "interval", [("floors", "double")]),
    "heart-rate": ("HeartRateRecord", "series", [("samples", "list:HeartRateRecord$Sample")]),
    "heart-rate-variability-rmssd": ("HeartRateVariabilityRmssdRecord", "instantaneous", [("heartRateVariabilityMillis", "double")]),
    "height": ("HeightRecord", "instantaneous", [("height", "measure:Length")]),
    "hydration": ("HydrationRecord", "interval", [("volume", "measure:Volume")]),
    "intermenstrual-bleeding": ("IntermenstrualBleedingRecord", "instantaneous", []),
    "lean-body-mass": ("LeanBodyMassRecord", "instantaneous", [("mass", "measure:Mass")]),
    "menstruation-flow": ("MenstruationFlowRecord", "instantaneous", [("flow", "constant:menstruation-flow")]),
    "menstruation-period": ("MenstruationPeriodRecord", "interval", []),
    "mindfulness-session": ("MindfulnessSessionRecord", "interval", [("mindfulnessSessionType", "constant:mindfulness-session-type"), ("title", "string?"), ("notes", "string?")]),
    "nutrition": ("NutritionRecord", "interval", [("energy", "measure:Energy?"), ("energyFromFat", "measure:Energy?"), *[(name, "measure:Mass?") for name in NUTRIENTS], ("name", "string?"), ("mealType", "constant:meal-type")]),
    "ovulation-test": ("OvulationTestRecord", "instantaneous", [("result", "constant:ovulation-test-result")]),
    "oxygen-saturation": ("OxygenSaturationRecord", "instantaneous", [("percentage", "percentage")]),
    "planned-exercise-session": ("PlannedExerciseSessionRecord", "interval", [("hasExplicitTime", "bool"), ("exerciseType", "constant:exercise-type"), ("title", "string?"), ("notes", "string?"), ("completedExerciseSessionId", "string?"), ("blocks", "list:PlannedExerciseBlock")]),
    "power": ("PowerRecord", "series", [("samples", "list:PowerRecord$Sample")]),
    "respiratory-rate": ("RespiratoryRateRecord", "instantaneous", [("rate", "double")]),
    "resting-heart-rate": ("RestingHeartRateRecord", "instantaneous", [("beatsPerMinute", "long")]),
    "sexual-activity": ("SexualActivityRecord", "instantaneous", [("protectionUsed", "constant:sexual-activity-protection-used")]),
    "skin-temperature": ("SkinTemperatureRecord", "series", [("baseline", "measure:Temperature?"), ("deltas", "list:SkinTemperatureRecord$Delta"), ("measurementLocation", "constant:skin-temperature-measurement-location")]),
    "sleep-session": ("SleepSessionRecord", "interval", [("title", "string?"), ("notes", "string?"), ("stages", "list:SleepSessionRecord$Stage")]),
    "speed": ("SpeedRecord", "series", [("samples", "list:SpeedRecord$Sample")]),
    "steps": ("StepsRecord", "interval", [("count", "long")]),
    "steps-cadence": ("StepsCadenceRecord", "series", [("samples", "list:StepsCadenceRecord$Sample")]),
    "total-calories-burned": ("TotalCaloriesBurnedRecord", "interval", [("energy", "measure:Energy")]),
    "weight": ("WeightRecord", "instantaneous", [("weight", "measure:Mass")]),
    "vo2-max": ("Vo2MaxRecord", "instantaneous", [("vo2MillilitersPerMinuteKilogram", "double"), ("measurementMethod", "constant:vo2-max-measurement-method")]),
    "wheelchair-pushes": ("WheelchairPushesRecord", "interval", [("count", "long")]),
}


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def json_name(prop: str, kind: str) -> str:
    base = kind.rstrip("?")
    if base.startswith("measure:"):
        return f"{snake(prop)}_{UNITS[base.split(':', 1)[1]]}"
    return snake(prop)


def record_fields(record_type: str) -> list[tuple[str, str]]:
    _cls, shape, fields = RECORDS[record_type]
    return (INSTANT_FIELDS if shape == "instantaneous" else INTERVAL_FIELDS) + fields


def model() -> dict:
    """The language-neutral model the Kotlin tests read alongside the vectors."""
    def fields(entries):
        return [{"property": p, "json": json_name(p, k), "kind": k.rstrip("?"), "nullable": k.endswith("?")} for p, k in entries]
    return {
        "sdk_version": SDK_VERSION,
        "projection_schema_version": SCHEMA_VERSION,
        "units": UNITS,
        "constants": {group: {"class": cls, "values": values} for group, (cls, values) in CONSTANTS.items()},
        "types": {name: fields(entries) for name, entries in TYPES.items()},
        "variants": {
            sealed: {kind: {"class": cls, "fields": fields(entries)} for kind, (cls, entries) in kinds.items()}
            for sealed, kinds in VARIANTS.items()
        },
        "records": {
            record_type: {"class": cls, "shape": shape, "fields": fields(record_fields(record_type))}
            for record_type, (cls, shape, _fields) in RECORDS.items()
        },
    }


# --- RFC 8785 canonical JSON --------------------------------------------------

def es_number(value: float | int) -> str:
    """ECMAScript Number::toString, which RFC 8785 requires for numbers."""
    if isinstance(value, bool):
        raise TypeError("booleans are not numbers")
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise ValueError("integer outside the IEEE-754 safe range")
        value = float(value)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    if value == 0:
        return "0"
    if value < 0:
        return "-" + es_number(-value)
    sign, digit_tuple, exponent = Decimal(repr(value)).as_tuple()
    digits = "".join(map(str, digit_tuple))
    stripped = digits.rstrip("0") or "0"
    exponent += len(digits) - len(stripped)
    k = len(stripped)
    n = k + exponent
    if k <= n <= 21:
        return stripped + "0" * (n - k)
    if 0 < n <= 21:
        return stripped[:n] + "." + stripped[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + stripped
    e = n - 1
    mantissa = stripped if k == 1 else stripped[0] + "." + stripped[1:]
    return f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"


def es_string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise ValueError("unpaired surrogate")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def utf16_key(key: str) -> list[int]:
    return list(key.encode("utf-16-be"))


def canonical(value) -> str:
    if value is None:
        raise ValueError("null is never projected")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return es_number(value)
    if isinstance(value, str):
        return es_string(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: utf16_key(item[0]))
        return "{" + ",".join(es_string(k) + ":" + canonical(v) for k, v in items) + "}"
    raise TypeError(f"unsupported value {type(value).__name__}")


def canonical_bytes(value) -> bytes:
    return canonical(value).encode("utf-8")


# --- JSON Schema -----------------------------------------------------------

INSTANT_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.([0-9]{3}|[0-9]{6}|[0-9]{9}))?Z$"
OFFSET_PATTERN = r"^[+-](0[0-9]|1[0-7]):[0-5][0-9]$|^[+-]18:00$"
DECIMAL_PATTERN = r"^(0|-?[1-9][0-9]*)$"


def kind_schema(kind: str) -> dict:
    if kind == "instant":
        return {"type": "string", "pattern": INSTANT_PATTERN}
    if kind == "offset":
        return {"type": "string", "pattern": OFFSET_PATTERN}
    if kind in ("duration", "long-string"):
        return {"type": "string", "pattern": DECIMAL_PATTERN}
    if kind == "string":
        return {"type": "string"}
    if kind == "bool":
        return {"type": "boolean"}
    if kind == "int":
        return {"type": "integer", "minimum": -(2**31), "maximum": 2**31 - 1}
    if kind == "long":
        return {"type": "integer", "minimum": -(2**53 - 1), "maximum": 2**53 - 1}
    if kind in ("double", "percentage") or kind.startswith("measure:"):
        return {"type": "number"}
    if kind.startswith("constant:"):
        group = kind.split(":", 1)[1]
        return {"type": "integer", "enum": sorted(set(CONSTANTS[group][1].values()))}
    if kind.startswith("object:"):
        return {"$ref": f"#/$defs/{def_name(kind.split(':', 1)[1])}"}
    if kind.startswith("list:"):
        return {"type": "array", "items": {"$ref": f"#/$defs/{def_name(kind.split(':', 1)[1])}"}}
    if kind.startswith("variant:"):
        return {"$ref": f"#/$defs/{def_name(kind.split(':', 1)[1])}"}
    if kind.startswith("list-variant:"):
        return {"type": "array", "items": {"$ref": f"#/$defs/{def_name(kind.split(':', 1)[1])}"}}
    raise ValueError(kind)


def def_name(sdk_type: str) -> str:
    return sdk_type.split(".")[-1].replace("$", "_")


def object_schema(entries: list[tuple[str, str]], extra: dict | None = None) -> dict:
    properties = dict(extra or {})
    required = sorted(extra or {})
    for prop, kind in entries:
        name = json_name(prop, kind)
        properties[name] = kind_schema(kind.rstrip("?"))
        if not kind.endswith("?"):
            required.append(name)
    return {"type": "object", "additionalProperties": False, "required": sorted(required), "properties": properties}


def referenced(kind: str) -> list[str]:
    base = kind.rstrip("?")
    for prefix in ("object:", "list:", "variant:", "list-variant:"):
        if base.startswith(prefix):
            return [base.split(":", 1)[1]]
    return []


def collect_defs(entries, defs: dict) -> None:
    for _prop, kind in entries:
        for name in referenced(kind):
            key = def_name(name)
            if key in defs:
                continue
            if name in VARIANTS:
                defs[key] = {"placeholder": True}
                options = []
                for variant_kind, (_cls, fields) in VARIANTS[name].items():
                    options.append(object_schema(fields, {"kind": {"const": variant_kind}}))
                    collect_defs(fields, defs)
                defs[key] = {"oneOf": options}
            else:
                defs[key] = {"placeholder": True}
                defs[key] = object_schema(TYPES[name])
                collect_defs(TYPES[name], defs)


def record_schema(record_type: str) -> dict:
    defs: dict = {}
    collect_defs(TYPES["metadata.Metadata"], defs)
    defs["Metadata"] = object_schema(TYPES["metadata.Metadata"])
    entries = record_fields(record_type)
    collect_defs(entries, defs)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:activity-tracker-portal:source-projection:{record_type}:{SCHEMA_VERSION}",
        "title": f"Source Projection for {record_type}, schema version {SCHEMA_VERSION}",
        "type": "object",
        "additionalProperties": False,
        "required": ["metadata", "projection_implementation_version", "projection_schema_version", "provider_version", "record", "record_type", "sdk_version"],
        "properties": {
            "record_type": {"const": record_type},
            "projection_schema_version": {"const": SCHEMA_VERSION},
            "projection_implementation_version": {"type": "string", "minLength": 1},
            "sdk_version": {"const": SDK_VERSION},
            "provider_version": {"type": "string", "minLength": 1},
            "metadata": {"$ref": "#/$defs/Metadata"},
            "record": object_schema(entries),
        },
        "$defs": dict(sorted(defs.items())),
    }


# --- Synthetic vectors -------------------------------------------------------

CONTEXT = {"implementation_version": "0.1.0-synthetic", "provider_version": "synthetic-provider-1"}
BASE_EPOCH = 1768462200  # 2026-01-15T07:30:00Z


def instant(minutes: float = 0, nanos: int = 0) -> str:
    """java.time.Instant.toString: fraction digits in groups of three."""
    from datetime import datetime, timezone

    seconds = BASE_EPOCH + int(minutes * 60)
    text = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if nanos == 0:
        fraction = ""
    elif nanos % 1_000_000 == 0:
        fraction = f".{nanos // 1_000_000:03d}"
    elif nanos % 1_000 == 0:
        fraction = f".{nanos // 1_000:06d}"
    else:
        fraction = f".{nanos:09d}"
    return text + fraction + "Z"


def metadata(serial: int, maximal: bool) -> dict:
    value = {
        "id": f"00000000-0000-4000-8000-{serial:012d}",
        "data_origin": {"package_name": "org.example.synthetic.source"},
        "last_modified_time": instant(90),
        "client_record_version": "0",
        "recording_method": 0,
    }
    if maximal:
        value |= {
            "client_record_id": f"synthetic-client-{serial}",
            "client_record_version": "9007199254740993",
            "recording_method": 2,
            "device": {"type": 1, "manufacturer": "Synthetic Devices", "model": "SD-1"},
        }
    return value


def times(record_type: str, maximal: bool) -> dict:
    shape = RECORDS[record_type][1]
    if shape == "instantaneous":
        value = {"time": instant(0, 250_000_000 if maximal else 0)}
        if maximal:
            value["zone_offset"] = "+05:30"
        return value
    value = {"start_time": instant(0), "end_time": instant(30)}
    if maximal:
        value |= {"start_zone_offset": "+05:30", "end_zone_offset": "+05:30"}
    return value


def location(minute: float, maximal: bool) -> dict:
    value = {"time": instant(minute), "latitude": 12.9715987, "longitude": 77.5945627}
    if maximal:
        value |= {"horizontal_accuracy_meters": 3.5, "vertical_accuracy_meters": 5.25, "altitude_meters": 920.75}
    return value


NUTRIENT_VALUES = {name: round(0.0005 * (index + 1), 7) for index, name in enumerate(NUTRIENTS)}
NUTRIENT_VALUES["biotin"] = 0.00003
NUTRIENT_VALUES["vitaminB12"] = 1e-7


def record_body(record_type: str, maximal: bool) -> dict:
    """Record-specific fields; the minimal form keeps only non-nullable ones."""
    t = lambda minute: instant(minute)
    bodies: dict[str, dict] = {
        "active-calories-burned": {"energy_calories": 245000.5},
        "basal-body-temperature": {"temperature_celsius": 36.45, "measurement_location": 1},
        "basal-metabolic-rate": {"basal_metabolic_rate_watts": 85.25},
        "blood-glucose": {"level_millimoles_per_liter": 5.6, "specimen_source": 1, "meal_type": 3, "relation_to_meal": 2},
        "blood-pressure": {"systolic_millimeters_of_mercury": 118.0, "diastolic_millimeters_of_mercury": 76.5, "body_position": 1, "measurement_location": 1},
        "body-fat": {"percentage": 21.4},
        "body-temperature": {"temperature_celsius": 36.8, "measurement_location": 2},
        "body-water-mass": {"mass_grams": 41000.25},
        "bone-mass": {"mass_grams": 3100.75},
        "cervical-mucus": {"appearance": 3, "sensation": 2},
        "cycling-pedaling-cadence": {"samples": [{"time": t(5), "revolutions_per_minute": 85.5}, {"time": t(10), "revolutions_per_minute": 90.0}]},
        "distance": {"distance_meters": 5234.75},
        "elevation-gained": {"elevation_meters": 120.5},
        "exercise-session": {
            "exercise_type": 56,
            "title": "Synthetic run é中",
            "notes": "synthetic notes",
            "segments": [
                {"start_time": t(0), "end_time": t(15), "segment_type": 46, "repetitions": 0},
                {"start_time": t(15), "end_time": t(30), "segment_type": 46, "repetitions": 0},
            ],
            "laps": [
                {"start_time": t(0), "end_time": t(10), "length_meters": 400.0},
                {"start_time": t(10), "end_time": t(20)},
            ],
            "exercise_route_result": {"kind": "data", "exercise_route": {"route": [location(5, True), location(10, False)]}},
            "planned_exercise_session_id": "synthetic-plan-1",
        },
        "floors-climbed": {"floors": 12.0},
        "heart-rate": {"samples": [{"time": t(5), "beats_per_minute": 72}, {"time": t(10), "beats_per_minute": 75}]},
        "heart-rate-variability-rmssd": {"heart_rate_variability_millis": 42.125},
        "height": {"height_meters": 1.785},
        "hydration": {"volume_liters": 0.75},
        "intermenstrual-bleeding": {},
        "lean-body-mass": {"mass_grams": 56000.5},
        "menstruation-flow": {"flow": 2},
        "menstruation-period": {},
        "mindfulness-session": {"mindfulness_session_type": 1, "title": "Synthetic breathing", "notes": "synthetic notes"},
        "nutrition": {
            "energy_calories": 650000.0,
            "energy_from_fat_calories": 210000.0,
            **{f"{snake(name)}_grams": value for name, value in NUTRIENT_VALUES.items()},
            "name": "Synthetic meal",
            "meal_type": 2,
        },
        "ovulation-test": {"result": 1},
        "oxygen-saturation": {"percentage": 97.5},
        "planned-exercise-session": {
            "has_explicit_time": True,
            "exercise_type": 56,
            "title": "Synthetic plan",
            "notes": "synthetic notes",
            "completed_exercise_session_id": "synthetic-session-1",
            "blocks": [
                {
                    "repetitions": 2,
                    "description": "synthetic block",
                    "steps": [
                        {
                            "exercise_type": 46,
                            "exercise_phase": 3,
                            "completion_goal": {"kind": "distance", "distance_meters": 1000.0},
                            "performance_targets": [
                                {"kind": "heart-rate", "min_heart_rate": 120.0, "max_heart_rate": 150.0},
                                {"kind": "speed", "min_speed_meters_per_second": 2.5, "max_speed_meters_per_second": 3.25},
                            ],
                            "description": "synthetic step",
                        },
                        {"exercise_type": 46, "exercise_phase": 2, "completion_goal": {"kind": "duration", "duration": "90000000000"}, "performance_targets": []},
                    ],
                }
            ],
        },
        "power": {"samples": [{"time": t(5), "power_watts": 180.5}, {"time": t(10), "power_watts": 210.0}]},
        "respiratory-rate": {"rate": 14.5},
        "resting-heart-rate": {"beats_per_minute": 58},
        "sexual-activity": {"protection_used": 1},
        "skin-temperature": {"baseline_celsius": 33.2, "deltas": [{"time": t(5), "delta_celsius": -0.25}, {"time": t(10), "delta_celsius": 0.5}], "measurement_location": 1},
        "sleep-session": {"title": "Synthetic night", "notes": "synthetic notes", "stages": [{"start_time": t(0), "end_time": t(10), "stage": 2}, {"start_time": t(10), "end_time": t(30), "stage": 4}]},
        "speed": {"samples": [{"time": t(5), "speed_meters_per_second": 2.75}, {"time": t(10), "speed_meters_per_second": 3.125}]},
        "steps": {"count": 4321},
        "steps-cadence": {"samples": [{"time": t(5), "rate": 160.5}, {"time": t(10), "rate": 162.0}]},
        "total-calories-burned": {"energy_calories": 1850000.25},
        "vo2-max": {"vo2_milliliters_per_minute_kilogram": 48.5, "measurement_method": 1},
        "weight": {"weight_grams": 72500.5},
        "wheelchair-pushes": {"count": 210},
    }
    body = copy.deepcopy(bodies[record_type])
    if not maximal:
        nullable = {json_name(p, k) for p, k in RECORDS[record_type][2] if k.endswith("?")}
        body = {k: v for k, v in body.items() if k not in nullable}
        for key, value in body.items():
            if isinstance(value, list) and key != "samples" and key != "deltas":
                body[key] = []
        if record_type == "exercise-session":
            body["exercise_route_result"] = {"kind": "no-data"}
    return body


def envelope(record_type: str, serial: int, record: dict, meta: dict, context: dict = CONTEXT) -> dict:
    return {
        "record_type": record_type,
        "projection_schema_version": SCHEMA_VERSION,
        "projection_implementation_version": context["implementation_version"],
        "sdk_version": SDK_VERSION,
        "provider_version": context["provider_version"],
        "metadata": meta,
        "record": record,
    }


def positive(name: str, record_type: str, projection: dict, tags: list[str], context: dict = CONTEXT) -> dict:
    data = canonical_bytes(projection)
    return {
        "name": name,
        "record_type": record_type,
        "tags": sorted(tags),
        "context": context,
        "projection": projection,
        "bytes": data.decode("utf-8"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def positives() -> list[dict]:
    vectors = []
    serial = 0
    for record_type in RECORDS:
        for maximal in (False, True):
            serial += 1
            record = times(record_type, maximal) | record_body(record_type, maximal)
            tags = ["coverage", "maximal" if maximal else "minimal"]
            if maximal:
                tags += ["envelope", "large-integer", "offset", "order"]
            else:
                tags += ["absent"]
            if any(kind.rstrip("?").startswith("measure:") or kind == "percentage" for _p, kind in RECORDS[record_type][2]):
                tags.append("units")
            if any(kind.rstrip("?").startswith("constant:") for _p, kind in RECORDS[record_type][2]):
                tags.append("constants")
            vectors.append(positive(f"{record_type}-{'maximal' if maximal else 'minimal'}", record_type, envelope(record_type, serial, record, metadata(serial, maximal)), tags))

    def variant_vector(name, record_type, mutate, tags, maximal=True):
        nonlocal serial
        serial += 1
        record = times(record_type, maximal) | record_body(record_type, maximal)
        mutate(record)
        vectors.append(positive(name, record_type, envelope(record_type, serial, record, metadata(serial, maximal)), tags))

    def route(kind):
        return lambda record: record.__setitem__("exercise_route_result", {"kind": kind})

    variant_vector("exercise-session-route-consent-required", "exercise-session", route("consent-required"), ["route", "variants"])
    variant_vector("exercise-session-route-no-data", "exercise-session", route("no-data"), ["route", "variants"])
    variant_vector("exercise-session-route-data", "exercise-session", lambda record: None, ["route", "variants", "order"])

    goals = [
        {"kind": "active-calories-burned", "active_calories_calories": 300000.0},
        {"kind": "distance-and-duration", "distance_meters": 5000.0, "duration": "1800000000000"},
        {"kind": "distance", "distance_meters": 1000.0},
        {"kind": "duration", "duration": "123456789"},
        {"kind": "manual-completion"},
        {"kind": "repetitions", "repetitions": 12},
        {"kind": "steps", "steps": 2000},
        {"kind": "total-calories-burned", "total_calories_calories": 500000.0},
    ]
    targets = [
        {"kind": "amrap"},
        {"kind": "cadence", "min_cadence": 150.0, "max_cadence": 170.0},
        {"kind": "heart-rate", "min_heart_rate": 120.0, "max_heart_rate": 150.0},
        {"kind": "power", "min_power_watts": 150.0, "max_power_watts": 250.0},
        {"kind": "rate-of-perceived-exertion", "rpe": 7},
        {"kind": "speed", "min_speed_meters_per_second": 2.5, "max_speed_meters_per_second": 3.25},
        {"kind": "weight", "mass_grams": 20000.0},
    ]

    def all_variants(record):
        steps = [
            {"exercise_type": 46, "exercise_phase": 3, "completion_goal": goal, "performance_targets": targets if index == 0 else []}
            for index, goal in enumerate(goals)
        ]
        record["blocks"] = [{"repetitions": 1, "steps": steps}]

    variant_vector("planned-exercise-session-every-variant", "planned-exercise-session", all_variants, ["variants", "duration", "order"])
    variant_vector("planned-exercise-session-date-only", "planned-exercise-session", lambda record: record.__setitem__("has_explicit_time", False), ["coverage"])

    def precision(record):
        record["start_time"] = instant(0, 123_000_000)
        record["end_time"] = instant(30, 123_456_789)
        record["samples"] = [{"time": instant(5, 1_000), "beats_per_minute": 72}, {"time": instant(10, 999_999_999), "beats_per_minute": 75}]

    variant_vector("heart-rate-instant-precision", "heart-rate", precision, ["precision"])

    def offsets(record):
        record["start_zone_offset"] = "+00:00"
        record["end_zone_offset"] = "-08:00"

    variant_vector("steps-zero-and-negative-offsets", "steps", offsets, ["offset"])

    def numbers(record):
        record["vitamin_b12_grams"] = 1e-7
        record["biotin_grams"] = 0.00003
        record["energy_calories"] = 12345678.125

    variant_vector("nutrition-number-forms", "nutrition", numbers, ["numbers", "units"])

    def text(record):
        record["title"] = "Quote \" backslash \\ tab \t newline \n control \u0001 emoji \U0001F3C3"

    variant_vector("sleep-session-text-escaping", "sleep-session", text, ["text"])

    serial += 1
    other = {"implementation_version": "9.9.9-other", "provider_version": "other-provider-7"}
    record = times("weight", True) | record_body("weight", True)
    vectors.append(positive("weight-other-context", "weight", envelope("weight", serial, record, metadata(serial, True), other), ["context"], other))
    return vectors


# Refusal vectors start from a positive vector's projection and apply one
# SDK-level change the Kotlin builder knows how to make.
REFUSALS = [
    {"name": "unknown-record-class", "failure": "unsupported-record-type", "base": "steps-minimal", "change": {"op": "custom-record"}, "path": ""},
    {"name": "nan-cadence-target", "failure": "non-finite-value", "base": "planned-exercise-session-every-variant", "change": {"op": "set-double", "path": "/record/blocks/0/steps/0/performance_targets/1/min_cadence", "value": "NaN"}, "path": "/record/blocks/0/steps/0/performance_targets/1/min_cadence"},
    {"name": "infinite-cadence-target", "failure": "non-finite-value", "base": "planned-exercise-session-every-variant", "change": {"op": "set-double", "path": "/record/blocks/0/steps/0/performance_targets/1/max_cadence", "value": "Infinity"}, "path": "/record/blocks/0/steps/0/performance_targets/1/max_cadence"},
    {"name": "undeclared-exercise-type", "failure": "undeclared-constant", "base": "exercise-session-minimal", "change": {"op": "set-int", "path": "/record/exercise_type", "value": 999}, "path": "/record/exercise_type"},
    {"name": "undeclared-sleep-stage", "failure": "undeclared-constant", "base": "sleep-session-maximal", "change": {"op": "set-int", "path": "/record/stages/1/stage", "value": 99}, "path": "/record/stages/1/stage"},
    {"name": "undeclared-cervical-mucus-appearance", "failure": "undeclared-constant", "base": "cervical-mucus-maximal", "change": {"op": "set-int", "path": "/record/appearance", "value": 99}, "path": "/record/appearance"},
    {"name": "unknown-completion-goal", "failure": "undeclared-variant", "base": "planned-exercise-session-maximal", "change": {"op": "unknown-variant", "path": "/record/blocks/0/steps/1/completion_goal"}, "path": "/record/blocks/0/steps/1/completion_goal"},
    {"name": "unknown-performance-target", "failure": "undeclared-variant", "base": "planned-exercise-session-maximal", "change": {"op": "unknown-variant", "path": "/record/blocks/0/steps/0/performance_targets/0"}, "path": "/record/blocks/0/steps/0/performance_targets/0"},
    {"name": "lone-surrogate-title", "failure": "unrepresentable-text", "base": "sleep-session-maximal", "change": {"op": "set-string-utf16", "path": "/record/title", "value": ["0x0041", "0xD800"]}, "path": "/record/title"},
    {"name": "year-10000", "failure": "unrepresentable-instant", "base": "steps-minimal", "change": {"op": "shift-years", "paths": ["/record/start_time", "/record/end_time"], "years": 7974}, "path": "/record/start_time"},
    {"name": "offset-with-seconds", "failure": "unrepresentable-offset", "base": "steps-maximal", "change": {"op": "offset-seconds", "path": "/record/start_zone_offset", "total_seconds": 3630}, "path": "/record/start_zone_offset"},
]


def refusals() -> list[dict]:
    names = {vector["name"] for vector in positives()}
    for refusal in REFUSALS:
        if refusal["base"] not in names:
            raise ValueError(f"unknown base vector {refusal['base']}")
    return copy.deepcopy(REFUSALS)


def schema_negatives(vectors: list[dict]) -> list[dict]:
    """Projections the shared schemas must reject."""
    by_name = {vector["name"]: vector for vector in vectors}
    out = []

    def add(name, base, mutate):
        projection = copy.deepcopy(by_name[base]["projection"])
        mutate(projection)
        out.append({"name": name, "record_type": by_name[base]["record_type"], "projection": projection})

    add("unknown-record-field", "steps-maximal", lambda p: p["record"].__setitem__("cadence", 1))
    add("unknown-envelope-field", "steps-maximal", lambda p: p.__setitem__("extra", "x"))
    add("missing-required-field", "steps-maximal", lambda p: p["record"].pop("count"))
    add("missing-metadata-id", "weight-maximal", lambda p: p["metadata"].pop("id"))
    add("null-optional-field", "sleep-session-maximal", lambda p: p["record"].__setitem__("title", None))
    add("undeclared-constant-value", "sleep-session-maximal", lambda p: p["record"]["stages"][0].__setitem__("stage", 99))
    add("offset-without-colon", "steps-maximal", lambda p: p["record"].__setitem__("start_zone_offset", "+0530"))
    add("instant-with-offset", "weight-maximal", lambda p: p["record"].__setitem__("time", "2026-01-15T13:00:00+05:30"))
    add("instant-two-fraction-digits", "weight-maximal", lambda p: p["record"].__setitem__("time", "2026-01-15T07:30:00.25Z"))
    add("client-record-version-number", "weight-maximal", lambda p: p["metadata"].__setitem__("client_record_version", 5))
    add("wrong-record-type", "weight-maximal", lambda p: p.__setitem__("record_type", "height"))
    add("unknown-variant-kind", "exercise-session-maximal", lambda p: p["record"].__setitem__("exercise_route_result", {"kind": "partial"}))
    add("variant-extra-field", "exercise-session-route-no-data", lambda p: p["record"]["exercise_route_result"].__setitem__("route", []))
    add("integer-as-string", "steps-maximal", lambda p: p["record"].__setitem__("count", "4321"))
    return out


# --- Files -----------------------------------------------------------------

def render(value) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def outputs() -> dict[str, str]:
    files: dict[str, str] = {"model.json": render(model())}
    for record_type in RECORDS:
        files[f"schemas/{record_type}.schema.json"] = render(record_schema(record_type))
    vectors = positives()
    files["vectors/positive.json"] = render({"sdk_version": SDK_VERSION, "vectors": vectors})
    files["vectors/refusals.json"] = render({"sdk_version": SDK_VERSION, "vectors": refusals()})
    files["vectors/schema-negatives.json"] = render({"sdk_version": SDK_VERSION, "vectors": schema_negatives(vectors)})
    manifest = {path: hashlib.sha256(text.encode("utf-8")).hexdigest() for path, text in sorted(files.items())}
    files["manifest.json"] = render({"generator": "projection/v1/tools/build_projection.py", "files": manifest})
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify checked-in files instead of writing them")
    args = parser.parse_args()
    files = outputs()
    stale = []
    for relative, text in files.items():
        path = ROOT / relative
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if args.check:
        expected = set(files)
        present = {str(p.relative_to(ROOT)) for p in ROOT.rglob("*.json")}
        stale += sorted(present - expected)
        if stale:
            print("stale projection files: " + ", ".join(sorted(stale)), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
