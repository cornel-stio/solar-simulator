import pandas as pd
import numpy as np
import pvlib
from pvlib.pvsystem import PVSystem, Array, FixedMount, SingleAxisTrackerMount
from pvlib.location import Location
from pvlib.modelchain import ModelChain
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
from timezonefinder import TimezoneFinder
import math
import itertools

class ArrayConfig(BaseModel):
    fix_mount: bool
    tilt: float = 0.0
    azimuth: float = 0.0
    axis_azimuth: float = 0.0
    max_angle: float = 0.0
    racking_model: str = "close_mount"
    available_area: float

class SimulationRequest(BaseModel):
    latitude: float
    longitude: float
    module_name: str
    inverter_name: str
    arrays: List[ArrayConfig]
    load_target_kwh: Optional[float] = None
    load_profile_name: str = "default_profile"
    is_commercial: bool = False  # <-- NEW: Tells the engine to use commercial logic

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


def read_load_curve(file_name):
    print(f"read load curve {file_name}")
    def completing_load_curve(default_df):
        default_df.index = pd.to_datetime(default_df.index, format='%d.%m.%Y %H:%M')

        year = default_df.index[0].year

        full_year_index = pd.date_range(
            start=f'{year}-01-01 00:00',
            end=f'{year}-12-31 23:45',
            freq='15min'
        )

        default_df = default_df.reindex(full_year_index)

        default_df['Sum [kWh]'] = default_df['Sum [kWh]'].fillna(default_df['Sum [kWh]'].shift(672))

        default_df.index = pd.to_datetime(default_df.index, format='%d.%m.%Y %H:%M')
        default_df.index.name = 'Time'

        return default_df

    file_address = f"Load Curve/{file_name}.csv"

    df = pd.read_csv(file_address, sep=";")

    df["Time"] = pd.to_datetime(df['Time'], format='%d.%m.%Y %H:%M')

    df = df.set_index('Time')

    files_missing_data = ["households default", "general business default", "farms default", "dairy farms default"]

    if file_name in files_missing_data:
        print("missing data filled out..")
        df = completing_load_curve(df)

    df = df.drop(df.columns[0], axis=1)

    df = df.resample("h").sum()

    return df


def prepare_load_curve(file_name, target_annual_kwh):
    df = read_load_curve(file_name)
    raw_hourly_load = df.iloc[:, 0].tolist()  # Convert the first column to a simple python list

    raw_total = sum(raw_hourly_load)
    scaling_factor = target_annual_kwh / raw_total

    scaled_hourly_load = [val * scaling_factor for val in raw_hourly_load]

    return scaled_hourly_load[:8760]


def simulate_battery(hourly_solar, hourly_load, battery_capacity_kwh):
    battery_capacity_w = battery_capacity_kwh * 1e3
    battery_charge = 0
    grid_import = 0
    soc_list = []  # NEW: Track the hour-by-hour State of Charge

    for i in range(len(hourly_solar)):
        hourly_load_w = hourly_load[i] * 1e3
        net_energy = hourly_solar[i] - hourly_load_w

        if net_energy > 0:
            battery_charge += net_energy
            if battery_charge > battery_capacity_w:
                battery_charge = battery_capacity_w
        elif net_energy < 0:
            battery_charge += net_energy
            if battery_charge < 0:
                grid_import += abs(battery_charge)
                battery_charge = 0

        # Record the SOC in kWh for this hour
        soc_list.append(battery_charge / 1e3)

    return grid_import / 1e3, soc_list


def calculate_optimal_battery(hourly_solar, hourly_load):
    daily_surpluses = []

    # Chunk the 8760 hours into 365 days of 24 hours
    for day in range(365):
        start_idx = day * 24
        end_idx = start_idx + 24

        day_solar = hourly_solar[start_idx:end_idx]
        day_load = hourly_load[start_idx:end_idx]

        # Calculate how much excess solar we had TODAY
        daily_surplus = 0
        for s, l in zip(day_solar, day_load):
            if s > l:
                daily_surplus += (s - l)

        daily_surpluses.append(daily_surplus)

    # Find the 90th percentile (ignore the top 10% of crazy sunny days)
    # Convert from Watts to kWh
    optimal_kwh = np.percentile(daily_surpluses, 90) / 1000

    return round(optimal_kwh, 1)


def find_commercial_partitions(total_panels, min_len, max_len):
    """for massive commercial roofs. Prevents RAM freezes."""
    strings = []

    # 1. Max out as many strings as possible
    while total_panels >= max_len:
        strings.append(max_len)
        total_panels -= max_len

    # 2. Handle the remainder safely
    if total_panels >= min_len:
        strings.append(total_panels)
    elif total_panels > 0 and len(strings) > 0:
        # Borrow panels from full strings to make the remainder valid
        needed = min_len - total_panels
        if needed <= len(strings) * (max_len - min_len):
            strings.append(total_panels)
            idx = 0
            while strings[-1] < min_len:
                if strings[idx] > min_len:
                    strings[idx] -= 1
                    strings[-1] += 1
                idx = (idx + 1) % (len(strings) - 1)
        else:
            # If it can't be balanced safely, drop the remainder (Industry standard for commercial)
            pass

    return [strings]  # Return as list of lists to match original architecture


def generate_multi_array_systems(
        array_configs,
        panel_specs,
        inverter_database,
        load_target_kwh=None,
        is_commercial=False,
        usable_space=0.8
        ):

    valid_system = []

    total_house_panels = 0
    for area in array_configs:
        total_house_panels += math.floor(area['available_area'] * usable_space / panel_specs['A_c'])

    if total_house_panels == 0:
        return []

    total_panel_dc_watts = total_house_panels * panel_specs['STC']

    # --- THE TRAFFIC COP ---
    # Automatically switch to Commercial Mode if >150 panels, or if user checked the box
    is_large_scale = is_commercial or (total_house_panels > 150)

    if is_large_scale:
        # COMMERCIAL: Grab large inverters (> 20kW) and calculate how many we need (Cascading)
        shortlisted_invs = inverter_database.T[inverter_database.T['Paco'] >= 20000]
    else:
        # RESIDENTIAL: Find a single inverter that perfectly matches the roof
        target_dc = total_panel_dc_watts if load_target_kwh is None else min((load_target_kwh / 1.3) * 1e3,
                                                                             total_panel_dc_watts)
        shortlisted_invs = get_shortlisted_inverters(target_dc, inverter_database)

    # Filter out inverters that can't handle the panel's amperage
    shortened_inv_database = shortlisted_invs[shortlisted_invs['Idcmax'] >= panel_specs['I_sc_ref']]

    for inverter_name, inverter in shortened_inv_database.iterrows():
        min_len = math.ceil(inverter['Mppt_low'] / panel_specs['V_mp_ref'])
        max_len = math.floor(inverter['Vdcmax'] / panel_specs['V_oc_ref'])

        all_faces_partitions = []

        # Calculate Inverter Cascading (How many inverters do we stack?)
        inverter_count = math.ceil(total_panel_dc_watts / inverter['Paco']) if is_large_scale else 1

        for area in array_configs:
            face_panels = math.floor(area['available_area'] * usable_space / panel_specs['A_c'])

            if is_large_scale:
                # FAST MATH: Prevents server freezes
                face_valid_string_groups = find_commercial_partitions(face_panels, min_len, max_len)
            else:
                # EXHAUSTIVE MATH: Perfect combinations for small roofs
                face_valid_string_groups = []
                for p_count in range(face_panels, 0, -1):
                    face_valid_string_groups = find_valid_partitions(p_count, min_len, max_len)
                    if face_valid_string_groups: break

            if not face_valid_string_groups:
                break
            all_faces_partitions.append(face_valid_string_groups)

        if len(all_faces_partitions) != len(array_configs):
            continue

            # Build the final combo
        for house_combo in itertools.product(*all_faces_partitions):
            # In commercial mode, we assume the cascaded inverters can handle the MPPTs
            if is_large_scale or hardware_can_support_multi(house_combo, inverter, panel_specs):
                valid_system.append({
                    "total_panels": total_house_panels,
                    "wiring_combo": house_combo,
                    "inverter_name": inverter_name,
                    "inverter_qty": inverter_count,  # NEW: Tell the UI how many inverters to buy!
                    "dc_ac_ratio": calculate_ratio(panel_specs, total_house_panels, inverter) / inverter_count
                })

    valid_system.sort(key=lambda x: (-x['total_panels'], abs(x['dc_ac_ratio'] - 1.20)))
    return valid_system[:3]


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

@app.get("/")
def serve_homepage():
    return FileResponse("index.html")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/load-profile-total")
def get_load_profile_total(profile_name: str):
    try:
        df = read_load_curve(profile_name)
        total_kwh = float(df.iloc[:, 0].sum())
        return {"status": "success", "total_kwh": round(total_kwh, 2)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/simulate")
def run_solar_simulation(request: SimulationRequest):
    print(f"Received request for Lat: {request.latitude}, Lon: {request.longitude}")

    try:
        weather, location = retrieve_weather(latitude=request.latitude, longitude=request.longitude)
        array_dicts = [array.model_dump() for array in request.arrays]

        panel_specs = CEC_MODULES[request.module_name]

        load_target_kwh = request.load_target_kwh

        print("Designing optimal system..")

        top_3_systems = generate_multi_array_systems(array_dicts, panel_specs, CEC_INVERTERS, load_target_kwh, request.is_commercial)

        if not top_3_systems:
            return {"status": "error", "message": "The drawn area is too small to fit any valid panel configurations."}

        results = []

        if load_target_kwh is None:
            for rank, system_config in enumerate(top_3_systems):
                model = create_system(
                    selected_mod=request.module_name,
                    best_config=system_config,
                    array_configs=array_dicts,
                    location=location,
                )
                model.run_model(weather)

                tz_offset = round(request.longitude / 15)

                raw_ac = model.results.ac.fillna(0).tolist()[:8760]

                result_ac = np.roll(raw_ac, tz_offset).tolist()

                annual_kwh = float(sum(result_ac) / 1000)

                results.append({
                    "rank": rank + 1,
                    "inverter": system_config["inverter_name"],
                    "total_panels": system_config["total_panels"],
                    "wiring_layout": system_config["wiring_combo"],
                    "dc_ac_ratio": system_config["dc_ac_ratio"],
                    "annual_kwh": round(annual_kwh, 2),
                    "status": "No load target provided",
                    "cover_percentage": None,
                    "recommended_battery_kwh": 0.0,
                    "inverter_qty": system_config["inverter_qty"],
                    "raw_data": {
                        "hourly_solar_watts": result_ac,
                        "hourly_load_watts": [0] * 8760,
                        "hourly_soc_kwh": [0] * 8760,
                    }
                })
        else:
            hourly_load = prepare_load_curve(request.load_profile_name, load_target_kwh)

            for rank, system_config in enumerate(top_3_systems):
                model = create_system(
                    selected_mod=request.module_name,
                    best_config=system_config,
                    array_configs=array_dicts,
                    location=location
                )
                model.run_model(weather)

                tz_offset = round(request.longitude / 15)

                # cap solar power to only 8760h
                raw_ac = model.results.ac.fillna(0).tolist()[:8760]
                result_ac = np.roll(raw_ac, tz_offset).tolist()

                annual_kwh = float(sum(result_ac) / 1000)

                optimal_bat_size = calculate_optimal_battery(result_ac, hourly_load)

                _, hourly_soc = simulate_battery(result_ac, hourly_load, optimal_bat_size)

                if load_target_kwh > annual_kwh:
                    status = "can't cover full load"
                    cover_percentage = round((annual_kwh / load_target_kwh) * 100, 1)
                else:
                    status = "can cover full load"
                    cover_percentage = 100

                results.append({
                    "rank": rank + 1,
                    "inverter": system_config["inverter_name"],
                    "total_panels": system_config["total_panels"],
                    "wiring_layout": system_config["wiring_combo"],
                    "dc_ac_ratio": system_config["dc_ac_ratio"],
                    "annual_kwh": round(annual_kwh, 2),
                    "status": status,
                    "cover_percentage": cover_percentage,
                    "recommended_battery_kwh": optimal_bat_size,
                    "inverter_qty": system_config["inverter_qty"],
                    "raw_data": {
                        "hourly_solar_watts": result_ac,
                        "hourly_load_watts": hourly_load,
                        "hourly_soc_kwh": hourly_soc,
                    }
                })

        return {
            "status": "success",
            "alternatives": results
        }

    except Exception as e:
        print(f"CRASH: {str(e)}")
        return {"status": "error", "message": str(e)}