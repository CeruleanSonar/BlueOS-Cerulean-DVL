FROM python:3.9-slim-bullseye
ENV PIP_ROOT_USER_ACTION=ignore
RUN pip install --upgrade pip
RUN apt update && apt install -y nmap git

# Create default user folder
RUN mkdir -p /home/pi

# Install dvl service
COPY dvl /home/pi/cerulean-dvl
RUN cd /home/pi/cerulean-dvl && pip3 install .

#Versioned Data
LABEL version="1.0.3"
LABEL permissions='\
    {\
    "NetworkMode": "host"\
    }'
LABEL authors='[\
    {\
    "name": "Nick Nothom",\
    "email": "nick.nothom@ceruleansonar.com"\
    }\
    ]'
LABEL company='{\
        "about": "",\
        "name": "Cerulean Sonar",\
        "email": "dennys.bisogno@ceruleansonar.com"\
    }'
LABEL type="device-integration"
LABEL tags='[\
    "positioning",\
    "navigation",\
    "doppler-velocity-log"\
]'
LABEL readme='https://raw.githubusercontent.com/CeruleanSonar/BlueOS-Cerulean-DVL/master/README.md'
LABEL links='{\
        "website": "https://ceruleansonar.com",\
        "support": "https://forum.ceruleansonar.com/categories"\
    }'

ENTRYPOINT /home/pi/cerulean-dvl/main.py
