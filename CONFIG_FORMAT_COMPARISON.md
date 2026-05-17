# Config Format Comparison: YAML vs JSON vs CSV

## The Problem We're Solving
Config needs to handle:
- Nested structures (peak hours with multipliers)
- Multiple location types with different settings
- Robot constraints (battery, capacity, return rules)
- Comments/documentation in the file

---

## Option 1: YAML (Current Proposal)

**File: `delivery_configs/rush_hour.yaml`**
```yaml
simulation:
  name: "Rush Hour Scenario"
  duration_hours: 24
  
delivery_generation:
  type: "poisson"
  base_rate: 0.5  # deliveries per minute
  peak_hours:
    lunch:
      hours: "11:00-13:00"
      multiplier: 3.0
    dinner:
      hours: "17:00-19:00"
      multiplier: 2.5

robot_constraints:
  num_robots: 10
  battery_capacity_kwh: 10
  battery_drain_per_km: 0.2
  max_capacity: 5  # max deliveries before return
  speed_mph: 5
  return_to_station_soc: 0.2  # return when battery below 20%
  
location_distribution:
  restaurants:
    count_sample: 50  # sample top 50 restaurants
  residences:
    distribution: "uniform"  # spread evenly across Glendale
```

**Pros:**
- Human-readable (almost English-like)
- Comments supported (`# like this`)
- Nested structures natural
- No quotes needed most of the time
- Popular in DevOps/infrastructure

**Cons:**
- Whitespace matters (indentation)
- Requires YAML library: `pip install pyyaml`
- Less standardized than JSON
- Can be ambiguous with types (is `yes` boolean or string?)

**Python to load:**
```python
import yaml
config = yaml.safe_load(open('rush_hour.yaml'))
base_rate = config['delivery_generation']['base_rate']
```

---

## Option 2: JSON (Standardized Alternative)

**File: `delivery_configs/rush_hour.json`**
```json
{
  "simulation": {
    "name": "Rush Hour Scenario",
    "duration_hours": 24
  },
  "delivery_generation": {
    "type": "poisson",
    "base_rate": 0.5,
    "peak_hours": {
      "lunch": {
        "hours": "11:00-13:00",
        "multiplier": 3.0
      },
      "dinner": {
        "hours": "17:00-19:00",
        "multiplier": 2.5
      }
    }
  },
  "robot_constraints": {
    "num_robots": 10,
    "battery_capacity_kwh": 10,
    "battery_drain_per_km": 0.2,
    "max_capacity": 5,
    "speed_mph": 5,
    "return_to_station_soc": 0.2
  },
  "location_distribution": {
    "restaurants": {
      "count_sample": 50
    },
    "residences": {
      "distribution": "uniform"
    }
  }
}
```

**Pros:**
- Universal standard (works everywhere)
- Built into Python: `import json` (no extra library)
- Strict syntax = fewer surprises
- Works with web/APIs naturally
- Type safety (quotes required for strings)

**Cons:**
- No comments (can't document why a value is set)
- More verbose (all the quotes and braces)
- Not as easy to read/write by hand
- Commas matter (easy to make syntax errors)

**Python to load:**
```python
import json
config = json.load(open('rush_hour.json'))
base_rate = config['delivery_generation']['base_rate']
```

---

## Option 3: CSV (Simpler, But Limited)

**File: `delivery_configs/rush_hour.csv`**
```csv
param_name,param_value
simulation.name,Rush Hour Scenario
simulation.duration_hours,24
delivery_generation.type,poisson
delivery_generation.base_rate,0.5
delivery_generation.peak_hours.lunch.hours,11:00-13:00
delivery_generation.peak_hours.lunch.multiplier,3.0
delivery_generation.peak_hours.dinner.hours,17:00-19:00
delivery_generation.peak_hours.dinner.multiplier,2.5
robot_constraints.num_robots,10
robot_constraints.battery_capacity_kwh,10
robot_constraints.battery_drain_per_km,0.2
robot_constraints.max_capacity,5
robot_constraints.speed_mph,5
robot_constraints.return_to_station_soc,0.2
location_distribution.restaurants.count_sample,50
location_distribution.residences.distribution,uniform
```

**Pros:**
- Simplest format (just rows)
- Excel-friendly (can edit in spreadsheet app)
- No special syntax
- Easy to parse (even without libraries): `csv` module

**Cons:**
- Awkward for nested structures (see above—dotted notation)
- No comments
- Hard to read/understand structure
- Doesn't handle arrays well (how do you represent 2 peak hours?)
- Unclear which values are which

**Python to load:**
```python
import csv
config_list = list(csv.DictReader(open('rush_hour.csv')))
config = {}
for row in config_list:
    # Reconstruct nested structure from dotted keys
    # This gets messy...
```

---

## Comparison Table

| Feature | YAML | JSON | CSV |
|---------|------|------|-----|
| **Readability** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ |
| **Nested structures** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ |
| **Comments** | ✓ Yes | ✗ No | ✗ No |
| **Built-in Python** | ✗ (needs pyyaml) | ✓ Yes | ✓ Yes |
| **Standardization** | Medium | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| **Hand-editable** | ✓ Easy | ~ Medium | ✓ Easy (but awkward) |
| **API-friendly** | Medium | ⭐⭐⭐⭐⭐ | Medium |
| **Array support** | ✓ Good | ✓ Good | ✗ Poor |

---

## Recommendation for Your Project

**Best choice: JSON**

**Why:**
1. **Built-in** - No extra dependencies (Python already has `json`)
2. **Standard** - If you ever want to call this from the Web API, JSON is natural
3. **Strict** - Forces you to be explicit (good for learning)
4. **Future-proof** - If you add a UI to create configs, JSON is easier to work with

**Why not YAML:**
- Adding a dependency (pyyaml) when you don't need to
- Slightly less strict (whitespace matters—easy to break accidentally)
- Won't help you understand JSON (which you'll need eventually)

**Why not CSV:**
- This config has nested structures—CSV is terrible for that
- You'd end up hacking around it anyway

---

## Your Config in JSON

```json
{
  "simulation": {
    "name": "Rush Hour Scenario",
    "duration_hours": 24,
    "start_time": "06:00"
  },
  "delivery_generation": {
    "type": "poisson",
    "base_rate": 0.5,
    "peak_hours": {
      "lunch": {
        "start_hour": 11,
        "end_hour": 13,
        "multiplier": 3.0
      },
      "dinner": {
        "start_hour": 17,
        "end_hour": 19,
        "multiplier": 2.5
      }
    }
  },
  "robot_constraints": {
    "num_robots": 10,
    "battery_capacity_kwh": 10.0,
    "battery_drain_per_km": 0.2,
    "max_deliveries_before_return": 5,
    "speed_mph": 5.0,
    "return_to_station_battery_percent": 20.0
  },
  "location_distribution": {
    "restaurants": {
      "sample_count": 50,
      "clustering": "high"
    },
    "residences": {
      "distribution": "uniform"
    }
  }
}
```

**Loading in Python:**
```python
import json

def load_simulation_config(config_path: str) -> dict:
    with open(config_path) as f:
        return json.load(f)

config = load_simulation_config('delivery_configs/rush_hour.json')
num_robots = config['robot_constraints']['num_robots']  # 10
speed = config['robot_constraints']['speed_mph']  # 5
```

---

## Edge Case: What if User Wants Comments?

If you want comments in JSON (which JSON doesn't support), you could:

1. **Use JSON5** (extended JSON with comments)
   ```javascript
   {
     // This is a comment
     "num_robots": 10  // edge case: trailing comma supported
   }
   ```
   - Library: `pip install json5`
   - But then you're adding a dependency again...

2. **Keep comments in a separate `config.txt` file**
   ```
   # Rush Hour Scenario
   # - Peak lunch hours: 11am-1pm (3x multiplier)
   # - Robots: 10 units
   # - See rush_hour.json for actual config
   ```

3. **Accept JSON as-is** - The parameter names are self-documenting
   - `"return_to_station_battery_percent": 20` is pretty clear

---

## Decision Tree

```
Does user need comments in config?
├─ YES → YAML (accept pyyaml dependency)
└─ NO → JSON (built-in, standard, simple)

For your project: JSON ✓
- Comments aren't critical
- Clean, standard format
- Easier to extend later
```

