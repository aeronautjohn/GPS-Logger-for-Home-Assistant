## GPS Logger
This is a simple app that logs GPS points to a MySQL database, but with a few smart features. For example, it only logs points if they're actually far enough from the most recently logged point to 'matter'. There's also integrated intelligent routing to 'fill in the blanks' between logged points for visualization later.

Installation instructions and an example visualization configuration is included in the Wiki.

## Prequisites:

You will need to install the following Add-Ons in Home Assistant:
`AppDaemon`
`MariaDB` (Or another MySQL server source, but these instructions will assume MariaDB)
`phpMyAdmin` (While not required; strongly recommended)

## Installation

1. After installing AppDaemon, navigate to settings -> Add-Ons -> AppDaemon -> Configuration and add `mysql-connector-python` to the Python packages field, then press “save”

2. Using phpMyAdmin, connect to your MariaDB server and create a new database. Create a user named ‘homeassistant’.

3. Locate your AppDaemon Apps folder. By default, it is found at /addon_configs/xxxxxxxx_appdaemon/apps

4. Add `gps_logger.py` to the apps folder.

5. Add the following to `apps.yaml`:

```
GPSLogger:
  module: gps_logger
  class: GPSLogger
  gps_lat_sensor: sensor.your_latitude_sensor
  gps_lon_sensor: sensor.your_longitude_sensor
  gps_speed_sensor: sensor.your_speed_sensor
  db_host: core-mariadb #Default for MariaDB
  db_user: homeassistant
  db_password: YOURDBPASSWORD
  db_name: YOURDBNAME
  speed_threshold: 5 # Default value. Uses whatever unit your GPS speed sensor outputs, so adjust accordingly
  route_distance: 150 # In meters. If a new point is logged and is further than this distance from the original, it'll calculate a route and log intermediate points

```

6. Restart AppDaemon and review logs and test!

## Configuration

All configuration for the app is done in the apps.yaml portion of AppDaemon, as added above.

`gps_lat_sensor:` Any sensor which provides GPS Latitude, such as a GPS dongle or an MQTT source.

`gps_lon_sensor:` Any sensor which provides GPS Longitude.

`db_host:` Your MySQL host. Ideally, this is something on the same machine as HomeAssistant.

`db_user:` The username of a user with privileges on the database you intend to use.

`db_password:` The password for the above mentioned user.

`db_name:` The database name you created in phpMyAdmin. **Warning:** I _strongly_ recommend creating a database _just_ for your GPS data. This can potentially create a very large number of entries and this is very beta software from a very amateur programmer; you can mitigate potential corruption issues by just giving this app its own database.

`log_distance:` A threshold, in meters, that needs to be met before the app will log new coordinates to the database. The default is 25 meters. This means that, by default, if the new `gps_latitude` and `gps_longitude` states are not more than 25 meters away from the most recent states found in the database, it will not log the points. This is one of the key features of this over just using a more basic automation or keeping track of states in the Home Assistant database. By logging this way and using this threshold, you won’t have thousands and thousands of logged points right next to each other in a parking lot, or similar.

`route_distance:` In the event that there are two points far from each other the app will attempt to generate coordinates between the most recent point and the point you’ve just logged. It does this so you don’t have long straight lines on your visualization later between distant points.

If Home Assistant shuts down or your dongle loses signal, you might travel a distance without any new logged points. This feature is intended to mitigate problems if that were to happen. This setting sets the threshold, in meters, after which it will attempt to route. The default is 50 meters.






