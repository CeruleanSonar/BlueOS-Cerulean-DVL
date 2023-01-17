# BlueOS-Cerulean-DVL

This is a docker implementation of a Cerulean DVL driver for the new Blue Robotics BlueOS.

To set this up, ssh into the Raspberry Pi (or access via `red-pill` in [BlueOS Terminal](https://docs.bluerobotics.com/ardusub-zola/software/onboard/BlueOS-1.0/advanced-usage/#terminal)) and run

`sudo docker run -d --net=host -v /root/.config/blueos:/root/.config --name=BlueOS-Cerulean-DVL --restart=unless-stopped nicknothom/blueos-cerulean-dvl:latest
`

The service will show in the "Available Services" section in BlueOS, where there are some configuration options.
