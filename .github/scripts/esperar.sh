#!/usr/bin/env bash
# Duerme hasta la hora de envío de hoy (TZ=America/Santiago). Un job de GitHub muere a las
# 6 h, así que cada llamada duerme como máximo 345 min; el workflow encadena dos tramos.
# Uso: esperar.sh [HH:MM]   (sin argumento: L-J 18:59, V 14:59; fin de semana no espera)
set -euo pipefail
MAX=$((345 * 60))
if [ -n "${1:-}" ]; then
  OBJ="$1:00"
else
  case "$(date +%u)" in
    1|2|3|4) OBJ="18:59:00" ;;
    5)       OBJ="14:59:00" ;;
    *)       echo "Fin de semana — sin espera."; exit 0 ;;
  esac
fi
ESPERA=$(( $(date -d "today $OBJ" +%s) - $(date +%s) ))
if [ "$ESPERA" -le 0 ]; then
  echo "$(date +%T) CLT: la hora objetivo ($OBJ) ya llegó o pasó — se continúa."
  exit 0
fi
TRAMO=$(( ESPERA < MAX ? ESPERA : MAX ))
echo "Ahora $(date +%T) CLT — faltan $((ESPERA / 60)) min para las $OBJ; este tramo duerme $((TRAMO / 60)) min."
sleep "$TRAMO"
echo "Despierto a las $(date +%T) CLT."
