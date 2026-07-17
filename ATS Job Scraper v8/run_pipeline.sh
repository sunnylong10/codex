#!/bin/bash
# One-click MNC job pipeline (Mac/Linux) — ENTER accepts defaults
cd "$(dirname "$0")"
[ -f venv/bin/activate ] && source venv/bin/activate

read -p "Parallel browser workers [3]: " W;          W=${W:-3}
read -p "Jitter minimum seconds [0.4]: " JMIN;       JMIN=${JMIN:-0.4}
read -p "Jitter maximum seconds [1.6]: " JMAX;       JMAX=${JMAX:-1.6}
read -p "Max newest jobs per company, 0=unlimited [400]: " MAXJ; MAXJ=${MAXJ:-400}
read -p "Self-heal fix rounds [2]: " ROUNDS;         ROUNDS=${ROUNDS:-2}
read -p "Country filter: 'all', one country, or comma list e.g. Singapore,Hong Kong [Singapore]: " LOC
LOC=${LOC:-Singapore}
read -p "Forget the retry blacklist and start fresh? y/N [n]: " FRESHQ
FRESH=""; [ "${FRESHQ,,}" = "y" ] && FRESH="--fresh"
LOCPART="--location \"$LOC\""
[ "${LOC,,}" = "all" ] && LOCPART="--all-locations"

echo
echo "Running: workers=$W jitter=$JMIN-${JMAX}s cap=$MAXJ rounds=$ROUNDS location=$LOC $FRESH"
echo ------------------------------------------------------------
python3 pipeline.py --max-rounds "$ROUNDS" $FRESH \
    --scraper-args "--workers $W --jitter $JMIN $JMAX --max-per-company $MAXJ $LOCPART"

echo; echo "================= PIPELINE FINISHED ================="
