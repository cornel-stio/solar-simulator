import pandas as pd
from retry_requests import retry
import pvlib
from pvlib.pvsystem import PVSystem, Array, FixedMount, SingleAxisTrackerMount
from pvlib.location import Location
from pvlib.modelchain import ModelChain
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
from timezonefinder import TimezoneFinder
from scipy.stats import false_discovery_control
import math
import itertools

class ArrayConfig(BaseModel):
    fix_mount: bool
    tilt: float = 0.0
    azimuth: float = 0.0
    axis_azimuth: float = 0.0
    max_angle: float = 0.0
    racking_model: str = "close_mount"

class SimulationRequest(BaseModel):
    latitude: float
    longitude: float
    module_name: str
    inverter_name: str
    arrays: List[ArrayConfig]

CEC_MODULES = pvlib.pvsystem.retrieve_sam("cecmod")
CEC_INVERTERS = pvlib.pvsystem.retrieve_sam("cecinverter")

# fetch pre-calculated weather data (pvgis)
# for now use this simple method, if the scripts work, then switch to retrieve raw weather data
def retrieve_weather(latitude, longitude):
    print(f"Downloading weather for {latitude}, {longitude}...")
    # FIXED: pvlib 0.13.0+ only returns two values (data, meta)
    weather_df, metadata = pvlib.iotools.get_pvgis_tmy(latitude, longitude, map_variables=True)

    tf = TimezoneFinder()
    tz_str = tf.timezone_at(lng=longitude, lat=latitude)
    if tz_str is None:
        tz_str = 'Asia/Jakarta'
    print(f"Detected Timezone: {tz_str}")

    weather_df.index = weather_df.index.tz_convert(tz_str)
    loc = Location(latitude=latitude, longitude=longitude, tz=tz_str)
    return weather_df, loc


def get_shortlisted_inverters(max_dc_watts, inverters):

    # target DC/AC ratio of 1.0 to 1.3
    # If DC is 8000W, an inverter should be between roughly 6000W and 8000W
    min_ac_power = max_dc_watts / 1.3
    max_ac_power = max_dc_watts / 1.0

    # 3. Filter the dataframe using Pandas (this takes milliseconds)
    # 'Paco' is the pvlib parameter for Max AC Power
    shortlist = inverters.T[
        (inverters.T['Paco'] >= min_ac_power) &
        (inverters.T['Paco'] <= max_ac_power)
        ]

    # need to work on cost

    return shortlist


def estimate_mppts(row, panel_specs):
    paco = row['Paco']  # Max AC Power in Watts
    idcmax = row['Idcmax']  # Max DC Current in Amps
    panel_ampere = panel_specs['I_sc_ref']

    # 1. Base guess on Power Class
    if paco < 600:
        guess = 1
    elif paco < 4000:
        guess = 1
    elif paco < 15000:
        guess = 2
    elif paco < 30000:
        guess = 3
    elif paco < 60000:
        guess = 4
    else:
        guess = 6

    # 2. Amperage Failsafe
    # Assume an average modern panel produces 14 Amps.
    # How many 14A strings can this inverter's total current handle?
    max_possible_strings = math.floor(idcmax / panel_ampere)

    # Return whichever is smaller: the power guess, or the physical wire limit
    return min(guess, max_possible_strings)


def find_valid_partitions(total_panels, min_len, max_len):
    """
    Finds all valid ways to divide 'total_panels' into strings.
    Every string must be >= min_len and <= max_len.
    """
    valid_configurations = []

    def partition_helper(remaining_panels, current_strings, current_max):

        if remaining_panels == 0:
            valid_configurations.append(current_strings)
            return

        if (remaining_panels > 0) and (remaining_panels < min_len):
            return

        start = min(remaining_panels, current_max, max_len)

        for string_size in range(start, min_len - 1, -1):

            partition_helper(
                remaining_panels - string_size,
                current_strings + [string_size],
                string_size
            )

    # Kick off the recursion with the total panels and an empty list of strings
    partition_helper(total_panels,[], max_len)

    return valid_configurations


def check_parallel(string):

    candidate_parallel = [list(g) for k, g in itertools.groupby(sorted(string))]
    is_parallel = len(candidate_parallel) < len(string)

    return candidate_parallel, is_parallel


def hardware_can_support_multi(house_combo, inverter, panel_specs):
    """
    Evaluates if a specific group of strings (e.g., [8, 8, 6]) can physically
    and safely plug into a specific inverter.
    """

    mppt_count = estimate_mppts(inverter, panel_specs)

    total_system_amps = 0
    candidate = {}
    roof_face = 1
    total_strings = 0
    is_parallel_ = False

    for face_strings in house_combo: # here face_strings is roof face
        num_strings = len(face_strings)
        total_strings += num_strings

        total_system_amps += (panel_specs['I_sc_ref'] * num_strings)

        candidate_parallel, is_parallel = check_parallel(face_strings)

        if is_parallel: # even if only one has parallel-connection possibility, it is enough to conduct check for mppt availability
            is_parallel_ = True

        candidate[roof_face] = candidate_parallel
        roof_face += 1

    if total_strings > mppt_count:
        if not is_parallel_:
            return False
        else:
            total_strings_now = 0
            for i in candidate.keys():
                total_strings_now += len(candidate[i])

            if total_strings_now > mppt_count:
                return False

    if total_system_amps > inverter['Idcmax']:
        return False

    return True


def calculate_ratio(panel_specs, panel_count, inverter):
    total_dc_watts = panel_count * panel_specs['STC']
    total_ac_watts = inverter['Paco']

    return round(total_dc_watts / total_ac_watts, 2)


def generate_multi_array_systems(array_configs, panel_specs, inverter_database, load_target_kwh):
    valid_system = []

    # calculate maximum panel amount and max power output
    total_house_panels = 0
    for area in array_configs:
        total_house_panels += math.floor(area['available_area'] / panel_specs['A_c'])
    total_panel_dc_watts = total_house_panels * panel_specs['STC']  # maximum power generated

    # see if load is given
    if load_target_kwh is None:
        total_dc_watts = total_panel_dc_watts
    else:
        max_load_watt = (load_target_kwh / 1.3) * 1e3 # this is maximum power used by the user
        total_dc_watts = min(max_load_watt, total_panel_dc_watts)

    inverter_database = get_shortlisted_inverters(total_dc_watts, inverter_database)
    shortened_inv_database = inverter_database[inverter_database['Idcmax'] >= panel_specs['I_sc_ref']]

    for inverter_name, inverter in shortened_inv_database.iterrows():
        min_len = math.ceil(inverter['Mppt_low'] / panel_specs['V_mp_ref'])  # Vmp is used for minimums
        max_len = math.floor(inverter['Vdcmax'] / panel_specs['V_oc_ref'])  # Voc is used for maximums

        all_faces_partitions = []
        total_house_panels = 0

        for area in array_configs:
            face_panels = math.floor(area['available_area'] / panel_specs['A_c'])
            face_valid_string_groups = []

            for p_count in range(face_panels, 0, -1): # go with the maximum amount first
                face_valid_string_groups = find_valid_partitions(p_count, min_len, max_len)

                if face_valid_string_groups: # if the "more panel" configuration can be satisfied, no need to check the rest
                    total_house_panels += p_count
                    break

            if not face_valid_string_groups:
                break

            all_faces_partitions.append(face_valid_string_groups)

        if len(all_faces_partitions) != len(array_configs):
            continue # change inverter

        for house_combo in itertools.product(*all_faces_partitions):
            if hardware_can_support_multi(house_combo, inverter, panel_specs):
                valid_system.append({
                    "total_panels": total_house_panels,
                    "wiring_combo": house_combo,
                    "inverter_name": inverter_name,
                    "dc_ac_ratio": calculate_ratio(panel_specs, total_house_panels, inverter)
                })

    valid_system.sort(key=lambda x: abs(x['dc_ac_ratio'] - 1.20))

    return valid_system


def create_system(selected_mod, best_config, array_configs, location):
    module_params = CEC_MODULES[selected_mod]
    inverter_params = CEC_INVERTERS[best_config['inverter_name']]
    temp_params = pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS['sapm']['open_rack_glass_glass']

    pvlib_arrays = []
    for array_config, strings in zip(array_configs, best_config['wiring_combo']):

        if array_config["fix_mount"]:
            mount = FixedMount(surface_tilt=array_config["tilt"], surface_azimuth=array_config["azimuth"])
        else:
            mount = SingleAxisTrackerMount(axis_tilt=0, axis_azimuth=array_config["axis_azimuth"],
                                           max_angle=array_config["max_angle"])

        for string_length in strings:
            pvlib_arrays.append(Array(
                mount=mount,
                module_parameters=module_params,
                temperature_model_parameters=temp_params,
                modules_per_string=string_length,
                strings=1,
            ))

    system = PVSystem(arrays=pvlib_arrays, inverter_parameters=inverter_params)
    return ModelChain(
        system,
        location=location,
        aoi_model="physical",
        spectral_model="no_loss",
    )

app = FastAPI(title="Indo Solar API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/simulate")
def run_solar_simulation(request: SimulationRequest):
    print(f"Received request for Lat: {request.latitude}, Lon: {request.longitude}")

    try:
        weather, location = retrieve_weather(latitude=request.latitude, longitude=request.longitude)
        array_dicts = [array.dict() for array in request.arrays]

        panel_specs = CEC_MODULES[request.module_name]

        print("Designing optimal system..")

        top_3_systems = generate_multi_array_systems(array_dicts, panel_specs, CEC_INVERTERS, load_target_kwh)

        results = []

        if load_target_kwh is None:
            for rank, system_config in enumerate(top_3_systems):
                model = create_system(system_config, array_dicts, request.module_name, location)
                model.run_model(weather)

                annual_kwh = float(model.results.ac.sum() / 1000)

                results.append({
                    "rank": rank + 1,
                    "inverter": system_config["inverter_name"],
                    "total_panels": system_config["total_panels"],
                    "wiring_layout": system_config["wiring_combo"],
                    "dc_ac_ratio": system_config["dc_ac_ratio"],
                    "annual_kwh": round(annual_kwh, 2)
                })
        else:
            for rank, system_config in enumerate(top_3_systems):
                model = create_system(system_config, array_dicts, request.module_name, location)
                model.run_model(weather)

                annual_kwh = float(model.results.ac.sum() / 1000)
                if load_target_kwh > annual_kwh:
                    status = "can't cover full load"
                    cover_percentage = annual_kwh / load_target_kwh
                else:
                    status = "can cover full load"
                    cover_percentage = 1

                results.append({
                    "rank": rank + 1,
                    "inverter": system_config["inverter_name"],
                    "total_panels": system_config["total_panels"],
                    "wiring_layout": system_config["wiring_combo"],
                    "dc_ac_ratio": system_config["dc_ac_ratio"],
                    "annual_kwh": round(annual_kwh, 2),
                    "status": status,
                    "cover_percentage": cover_percentage,
                })

        return {
            "status": "success",
            "alternatives": results
        }

    except Exception as e:
        print(f"CRASH: {str(e)}")
        return {"status": "error", "message": str(e)}