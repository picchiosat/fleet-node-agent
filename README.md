# 🤖 Fleet Control Agent

🌍 *[Read in English](#english) | 🇮🇹 [Leggi in Italiano](#italiano)*

---

<a name="english"></a>
## 🇬🇧 English

The **Fleet Control Agent** is a lightweight monitoring script designed for remote nodes (Raspberry Pi / Linux). It collects hardware telemetry and executes commands sent from the Central Dashboard via MQTT.

### ✨ Features
* **Real-Time Telemetry:** Monitors CPU usage, RAM, Temperature, and Disk space.
* **Service Management:** Remote Start, Stop, or Restart of system daemons (MMDVMHost, DMRGateway, etc.).
* **Smart Auto-Healing:** Automatically detects crashed services and attempts to revive them.
* **Hardware Reset (GPIO):** Physically reboot the MMDVM radio HAT via GPIO pins directly from the dashboard.

### 🚀 Quick Installation

1. **Clone the repo:**

        git clone https://git.arifvg.it/iv3jdv/fleet-control-agent.git web-control
        cd web-control

2. **Install dependencies:**

        pip install -r requirements.txt

3. **Configure:** Edit `node_config.json` with your MQTT credentials and a unique `client_id`.
4. **Run:** `python3 system_monitor.py`

---

<a name="italiano"></a>
## 🇮🇹 Italiano

Il **Fleet Control Agent** è uno script di monitoraggio leggero progettato per i nodi remoti (Raspberry Pi / Linux). Raccoglie la telemetria hardware ed esegue i comandi inviati dalla Dashboard Centrale tramite protocollo MQTT.

### ✨ Funzionalità
* **Telemetria in Tempo Reale:** Monitoraggio di utilizzo CPU, RAM, Temperatura e spazio su Disco.
* **Gestione Servizi:** Avvio, arresto o riavvio remoto dei demoni di sistema (MMDVMHost, DMRGateway, ecc.).
* **Auto-Healing Intelligente:** Rileva automaticamente i servizi andati in blocco e tenta di riavviarli autonomamente.
* **Reset Hardware (GPIO):** Invia un impulso di reset fisico alla scheda radio MMDVM tramite i pin GPIO direttamente dalla dashboard.

### 🚀 Installazione Rapida
1. **Clona il repository:**

        git clone https://git.arifvg.it/iv3jdv/web-control-agent.git web-control
        cd web-control

2. **Installa le dipendenze:**

        pip install -r requirements.txt

3. **Configurazione:** Modifica il file `node_config.json` inserendo le tue credenziali MQTT e un `client_id` univoco.
4. **Avvio:** `python3 system_monitor.py`

---
*Created by IV3JDV @ ARIFVG - 2026*
