# Solar Simulator

**A full-stack, geospatial solar and battery simulation engine.**

Solar Simulator bridges the gap between residential solar calculators and commercial-grade engineering software. It allows users to map real-world roof geometry, process 8,760-hour localized weather and load data, and instantly run complex combinatorial physics simulations to determine optimal hardware sizing, stringing combinations, and battery logic.

**Live Demo:** [https://solar-simulator-cstio.vercel.app/](https://solar-simulator-cstio.vercel.app/)

---

## 🚀 Key Features

*   **Interactive Geospatial Mapping:** Built with Leaflet, users can search for any address, draw polygon roof boundaries over satellite imagery, and set exact roof azimuths and tilts.
*   **Hybrid Physics Engine (Residential & Commercial):** 
    *   *Residential Scale:* Uses exhaustive combinatorial search to find the perfect MPPT stringing math for standard homes.
    *   *Commercial Scale:* Automatically detects large systems (>150 panels) and switches to a lightning-fast greedy heuristic algorithm with automated **Inverter Cascading** to prevent memory freezing.
*   **8,760-Hour Time-Series Simulation:** Pulls raw hourly irradiance data from the European Commission's PVGIS API, shifted dynamically to match local timezones based on longitude.
*   **Dynamic Load Curve Normalization:** Automatically scales 15-minute timestamped template CSVs into precise 8,760-hour continuous arrays to map against solar generation.
*   **Battery State-of-Charge (SOC) Logic:** Calculates optimal battery sizing and simulates hour-by-hour charging and discharging against localized household consumption.
*   **Interactive Dashboard (Cross-Filtering):** Features dynamic Chart.js visualizations. Clicking a specific month on the bar chart recalculates and redraws the 24-hour average load and generation curves for that specific month.

---

## 📸 Screenshots

<img width="959" height="503" alt="Screenshot 2026-07-13 224208" src="https://github.com/user-attachments/assets/e45048e1-fc74-44ab-a054-be19c21eb0ad" />

---

## 🛠️ Tech Stack

**Backend (Physics & Data Engine):**
*   **Python 3**
*   **FastAPI:** High-performance async API routing.
*   **pvlib:** Industry-standard solar physics calculations.
*   **Pandas & NumPy:** Time-series array manipulation and interpolation.
*   **Uvicorn:** ASGI web server.

**Frontend (UI & Visualization):**
*   **Vanilla JavaScript / HTML5 / CSS3**
*   **Leaflet.js & Leaflet-Draw:** Geospatial mapping and polygon area calculation.
*   **Chart.js:** Interactive, multi-axis data visualization.
*   **OpenStreetMap Nominatim API:** Geocoding and location search.

---

## ⚙️ Local Installation & Setup

To run this application locally, you will need Python installed on your machine.

**1. Clone the repository**
```bash
git clone https://github.com/YourUsername/indo-solar-pro-engine.git
cd indo-solar-pro-engine
```

**2. Install dependencies**
It is recommended to use a virtual environment.
```bash
pip install -r requirements.txt
```

**3. Run the backend server**
```bash
uvicorn main:app --reload
```
*The FastAPI server will boot up and listen on [http://127.0.0.1:8000](http://127.0.0.1:8000).*

**4. Launch the Frontend**
Simply open the `index.html` file in any modern web browser.

---

## 📂 Project Structure

*   `main.py`: The FastAPI backend, physics engine, combinatorial stringing logic, and PVGIS data parser.
*   `index.html`: The complete frontend UI, map logic, API fetch calls, and Chart.js rendering.
*   `Load Curve/`: Directory containing the 15-minute and hourly consumption CSV templates.
*   `requirements.txt`: Python package dependencies.
