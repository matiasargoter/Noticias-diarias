#!/bin/bash
# =====================================================================
# setup.sh — Configura la automatización "Noticias Diarias"
# Ejecutar UNA sola vez desde Terminal:
#   bash "/Users/matiasargote/Desktop/Noticias diarias/setup.sh"
# =====================================================================
set -e

SCRIPT_DIR="/Users/matiasargote/Desktop/Noticias diarias"
PLIST_FILE="com.noticias.diarias.plist"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     Setup: Noticias Diarias Chile        ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# 1. Verificar Python
echo "▸ Python: $(python3 --version) — $(/usr/bin/python3 -c 'import sys; print(sys.executable)')"

# 2. Instalar dependencias
echo "▸ Instalando dependencias Python (requests, beautifulsoup4, lxml)…"
/usr/bin/python3 -m pip install --quiet requests beautifulsoup4 lxml
echo "  ✓ Dependencias instaladas"

# 3. Crear directorio de logs
mkdir -p "$SCRIPT_DIR/logs"
echo "  ✓ Directorio logs/ creado"

# 4. Hacer el script ejecutable
chmod +x "$SCRIPT_DIR/noticias_diarias.py"

# 5. Copiar plist a LaunchAgents
mkdir -p "$LAUNCH_AGENTS"
cp "$SCRIPT_DIR/$PLIST_FILE" "$LAUNCH_AGENTS/"
echo "  ✓ Plist copiado a $LAUNCH_AGENTS/"

# 6. Descargar si ya está cargado y volver a cargar
launchctl unload "$LAUNCH_AGENTS/$PLIST_FILE" 2>/dev/null || true
launchctl load   "$LAUNCH_AGENTS/$PLIST_FILE"
echo "  ✓ LaunchAgent cargado"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  PASO FINAL (manual): Agregar contraseña de app Gmail        ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
echo "║  1. Ve a: myaccount.google.com → Seguridad → Verificación   ║"
echo "║     en 2 pasos → Contraseñas de aplicaciones               ║"
echo "║                                                              ║"
echo "║  2. Crea una nueva contraseña de app (ej: 'Noticias Mac')   ║"
echo "║                                                              ║"
echo "║  3. Abre el archivo:                                         ║"
echo "║     Noticias diarias/noticias_diarias.py                     ║"
echo "║     y reemplaza PONER_AQUI_CONTRASEÑA_DE_APP                ║"
echo "║     por la clave de 16 caracteres que te dio Google          ║"
echo "║                                                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Horario: Lunes a Viernes a las 18:30"
echo "  Correo destino: matiasargoter@gmail.com"
echo "  Logs: $SCRIPT_DIR/logs/"
echo ""
echo "  Para probar ahora (sin esperar las 18:30):"
echo "  python3 \"$SCRIPT_DIR/noticias_diarias.py\""
echo ""
