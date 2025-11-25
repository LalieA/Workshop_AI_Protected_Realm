# Copyright (C) 2025 CEA - All Rights Reserved
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of  MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.

FROM python:3.10-bullseye
WORKDIR /app
COPY . .

RUN apt-get update && apt-get install -y --no-install-recommends locales \
 && rm -rf /var/lib/apt/lists/*

# Generate the en_US.UTF-8 locale
RUN echo "en_US.UTF-8 UTF-8" > /etc/locale.gen && locale-gen

# Set default locale environment variables
ENV LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8

RUN pip install -r requirements.txt
CMD ["sh", "-c", "python3 ml_anomaly_score.py -v & python3 mqtt_subscriber.py -v"]
