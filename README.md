# Multi-brand robot control portal on Open-RMF

A web portal for monitoring and commanding robots from **different vendors** at the
same site, built on [Open-RMF](https://www.open-rmf.org/). There's no Gazebo,
no digital twin and no 3D model.

```
 Browser ──► portal/ (FastAPI + static UI)          ← this repo
                │   holds the RMF token, exposes only a fixed set of commands
                ▼
          rmf-web api-server  (open-rmf/rmf-web)     ← run as-is
                │   REST ⇄ ROS 2, stores task/fleet history
                ▼
          Open-RMF core (headless)                   ← launch/rmf_core.launch.xml
          traffic schedule · task dispatcher (bidding) · supervisors
                │
     ┌──────────┴───────────┬────────────────────┐
 fleet adapter "brand_a" fleet adapter "brand_b"  fleet adapter "mir"   ← this repo
 driver: generic_rest    driver: generic_rest     driver: mir             (one per brand)
     │                       │                        │
 vendor bridge/robot     vendor bridge/robot      MiR REST API on robot
```

## What you need (and what you don't)

**You don't need** a simulation, Gazebo world or digital twin. The rmf_demos
repository uses simulation only for demos.

**You do need:**

1. **A navigation graph** (`maps/site/nav_graph.yaml`). RMF plans routes and
   avoids conflicts between fleets on a 2D graph of waypoints and lanes. It
   can't coordinate robots without one. It's a small YAML file. You can
   write it by hand (as in this repo) or draw it over a floor-plan image
   with [rmf_site](https://github.com/open-rmf/rmf_site) or traffic-editor.
2. **One fleet adapter per brand.** RMF doesn't talk to robots directly. Each
   fleet adapter turns RMF commands ("go to waypoint X", "stop", "do action
   Y") into calls to the vendor's API. In this repo, that part is a small
   *driver* class per brand. Each robot still uses its own localization and
   navigation stack. RMF sends it goals; it doesn't drive the motors.
3. **Robots that expose a network API** for pose, battery, "go to (x, y, yaw)",
   stop, and "is the command finished?". Most commercial AMRs have this: REST,
   gRPC, MQTT, a ROS topic or a fleet manager. Robots with only a closed
   remote control can't be integrated.
4. **A shared coordinate frame.** Each vendor's map has its own origin. For
   each fleet, you give 2 or more matching points (`reference_coordinates` in
   the fleet config). The adapter then converts between the vendor's frame and
   the nav graph's frame.

Before you write a driver, check whether an adapter already exists for your
brand. For example, [free_fleet](https://github.com/open-rmf/free_fleet)
covers ROS 2 / Nav2 robots, and there are community adapters for some
vendors.

## Repository layout

| Path | What it is |
|---|---|
| `fleet_adapters/multi_brand_fleet_adapter/` | ROS 2 package. EasyFullControl fleet adapter (based on `open-rmf/fleet_adapter_template`) with pluggable per-brand drivers |
| `…/multi_brand_fleet_adapter/robot_api.py` | The interface every brand driver implements |
| `…/drivers/generic_rest.py` | Driver for any robot or bridge that speaks a small REST contract (documented in the file) |
| `…/drivers/mir.py` | Driver for MiR robots (REST API v2.0.0). Verify it against your MiR software version |
| `…/config/*.yaml` | One config per fleet: limits, footprint, battery, chargers, driver settings |
| `…/launch/rmf_core.launch.xml` | Headless RMF core (no RViz/Gazebo) |
| `…/launch/site.launch.xml` | Core plus one adapter per fleet |
| `maps/site/nav_graph.yaml` | Example hand-written navigation graph |
| `portal/` | Web portal: FastAPI backend-for-frontend plus a UI with no build step |
| `tools/mock_robot_server.py` | Fake robots that speak the generic REST contract, and a reference for writing a real bridge |
| `tools/mock_rmf_api.py` | Stand-in for rmf-web's api-server, for UI work without ROS. It has no traffic management |

## Portal features

- Live list of every fleet and robot: status, battery, location, current task and issues
- A top-down site plan drawn from the nav graph, showing robot positions and headings
- **Go to place** for a specific robot (RMF direct robot task)
- **Patrol** dispatch: RMF picks the best robot across all brands through bidding. You can also pin a fleet or a robot
- Cancel a task. Take a robot out of service and put it back (decommission/recommission, with task reassignment)
- Optional operator login (HTTP basic). The browser never holds an RMF credential

## Quick start 1: portal with mock robots (no ROS, about 1 minute)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r portal/requirements.txt
./tools/run_mock_demo.sh           # then open http://127.0.0.1:8080
```

## Quick start 2: real Open-RMF, mock robots (no hardware, no simulation)

Requires ROS 2 and Open-RMF binaries. See the
[Open-RMF install guide](https://github.com/open-rmf/rmf#installation) for
supported ROS 2 distros.

```bash
# 1. Build the fleet adapter
mkdir -p ~/rmf_ws/src && cd ~/rmf_ws/src
ln -s /path/to/this/repo/fleet_adapters/multi_brand_fleet_adapter .
cd ~/rmf_ws && colcon build --packages-select multi_brand_fleet_adapter
source install/setup.bash
pip install nudged           # only needed if you use reference_coordinates

# 2. Fake robots (replace with real robots/bridges later)
python3 /path/to/repo/tools/mock_robot_server.py --port 7001

# 3. rmf-web api-server, from a clone of open-rmf/rmf-web
#    (follow that repo's README: pnpm install / pipenv, then start the api-server).
#    It listens on :8000 and fleet adapters connect to ws://localhost:8000/_internal

# 4. RMF core + both fleet adapters
ros2 launch multi_brand_fleet_adapter site.launch.xml \
    nav_graph:=/path/to/repo/maps/site/nav_graph.yaml

# 5. Portal
cd /path/to/repo/portal && cp .env.example .env   # edit it
set -a && . ./.env && set +a && python3 server.py
```

## Adding a new robot brand

1. **Driver.** Subclass `RobotAPI` in `drivers/<brand>.py`. Implement
   `get_data`, `navigate`, `stop` and `is_command_completed` (plus
   `start_activity` and `localize` if needed). Register it in
   `drivers/__init__.py`. Unit test it the way `test/test_drivers.py` does. No
   ROS is needed.
   *Or* skip the driver: write a small bridge next to the robot that exposes
   the `generic_rest` contract, and use `driver: generic_rest`.
2. **Config.** Copy `config/brand_a_fleet.yaml`. Set the fleet name, the real
   speed/footprint/battery values, the robot names with their charger
   waypoints, `fleet_manager.driver`, and `reference_coordinates`.
3. **Launch.** Add a `<node>` for the new config in `site.launch.xml`.
4. **Nav graph.** Make sure every lane the new robots use is in the graph and
   that their chargers exist as `is_charger` waypoints.

The portal needs no changes. New fleets and robots appear automatically.

## Tests

```bash
pip install pytest requests
cd fleet_adapters/multi_brand_fleet_adapter && python3 -m pytest -q test
```

## Before production

- Change `jwt_secret` on the api-server, or switch it to your identity
  provider's public key. Then set `RMF_JWT_*` or `RMF_API_TOKEN` to match.
- Set `PORTAL_USER`/`PORTAL_PASSWORD` or put the portal behind SSO, and serve it over HTTPS.
- Tune each fleet's `limits`, `profile` and battery values to the real robots.
  RMF's schedule is only as good as these numbers.
- Test on the real site with people clear of the robots. Keep vendor e-stops in reach.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pieces communicate.
