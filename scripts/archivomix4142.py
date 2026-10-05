import os
import zipfile
import pandas as pd
import glob
import shutil
import numpy as np
import time
import argparse
from datetime import datetime


# =========================
# CONFIGURACIÓN
# =========================
DIRECTORIO_BASE = os.environ.get("LC_4142_REPORTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "datos", "4142"))
DIRECTORIO_PLANO = os.environ.get("LC_4142_OUTPUT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "salida"))


def esperar_archivo_completo(ruta, intentos=10, espera=1):
    ultimo_tamano = -1

    for _ in range(intentos):
        if not os.path.exists(ruta):
            time.sleep(espera)
            continue

        tamano = os.path.getsize(ruta)

        if tamano > 0 and tamano == ultimo_tamano:
            return True

        ultimo_tamano = tamano
        time.sleep(espera)

    return False


def fusionar_tienda_32_a_28_mix(directorio):
    print("\n🔄 Fusionando tienda 32 → 28 en MIX...")

    if not os.path.exists(directorio):
        print(f"⚠️ No existe directorio MIX: {directorio}")
        return

    archivos = os.listdir(directorio)

    for archivo in archivos:
        if "420032" in archivo and archivo.endswith(".txt") and "mix" in archivo.lower():
            ruta_32 = os.path.join(directorio, archivo)
            archivo_28 = archivo.replace("420032", "420028")
            ruta_28 = os.path.join(directorio, archivo_28)

            if os.path.exists(ruta_28):
                print(f"➕ Fusionando {archivo} → {archivo_28}")

                productos = {}
                header = None

                def procesar(ruta, es_28=False):
                    nonlocal header

                    with open(ruta, 'r', encoding='utf-8') as f:
                        for linea in f:
                            partes = linea.strip().split('\t')
                            if not partes:
                                continue

                            if partes[0] == 'H':
                                if es_28:
                                    header = partes

                            elif partes[0] == 'D' and len(partes) >= 7:
                                tipo = partes[1]
                                hora = partes[2]
                                codigo = str(partes[3]).strip()

                                try:
                                    cantidad = float(partes[4])
                                except Exception:
                                    cantidad = 0.0

                                try:
                                    importe = float(partes[6])
                                except Exception:
                                    importe = 0.0

                                clave = codigo

                                if clave not in productos:
                                    productos[clave] = {
                                        "tipo": tipo,
                                        "hora": hora,
                                        "codigo": codigo,
                                        "cantidad": 0.0,
                                        "importe": 0.0
                                    }

                                productos[clave]["cantidad"] += cantidad
                                productos[clave]["importe"] += importe

                procesar(ruta_28, es_28=True)
                procesar(ruta_32, es_28=False)

                if header:
                    header[1] = "420028"

                with open(ruta_28, 'w', encoding='utf-8') as f:
                    if header:
                        f.write('\t'.join(header) + '\n')

                    for prod in sorted(productos.values(), key=lambda x: x["codigo"]):
                        cantidad = prod["cantidad"]
                        cantidad_str = str(int(cantidad)) if float(cantidad).is_integer() else str(round(cantidad, 2))
                        precio_final = round(prod["importe"] / prod["cantidad"], 2) if prod["cantidad"] else 0.0
                        importe_final = round(prod["importe"], 2)

                        linea = [
                            'D',
                            prod["tipo"],
                            prod["hora"],
                            prod["codigo"],
                            cantidad_str,
                            f'{precio_final:.2f}',
                            f'{importe_final:.2f}'
                        ]

                        f.write('\t'.join(linea) + '\n')

                os.remove(ruta_32)
                print(f"🗑️ Eliminado: {archivo}")

            else:
                print(f"⚠️ No existe archivo 28 para combinar con {archivo}")


def obtener_ultima_carpeta_fecha(directorio_base):
    try:
        carpetas = [
            carpeta for carpeta in os.listdir(directorio_base)
            if os.path.isdir(os.path.join(directorio_base, carpeta))
        ]

        carpetas_fechadas = [
            carpeta for carpeta in carpetas
            if len(carpeta) == 10 and all(c.isdigit() for c in carpeta.replace('-', ''))
        ]

        carpetas_fechadas.sort(
            key=lambda fecha: datetime.strptime(fecha, '%Y-%m-%d'),
            reverse=True
        )

        if carpetas_fechadas:
            return os.path.join(directorio_base, carpetas_fechadas[0])

        print("❌ No se encontraron carpetas con formato de fecha.")
        return None

    except Exception as e:
        print(f"❌ Error al identificar la última carpeta por fecha: {e}")
        return None


def transformar_fecha(fecha):
    try:
        partes = str(fecha).split('-')
        if len(partes) == 3:
            return f"{partes[1]}/{partes[2]}/{partes[0]}"
        return str(fecha)
    except Exception:
        return str(fecha)


def to_numeric_clean(series):
    s = series.astype(str)
    s = s.str.strip()
    s = s.str.replace(r'^\((.*)\)$', r'-\1', regex=True)
    s = s.str.replace('$', '', regex=False).str.replace(',', '', regex=False)
    s = s.str.strip()
    return pd.to_numeric(s, errors='coerce')


def formatear_numero_franquicia(franchise_store):
    try:
        franchise_store = str(franchise_store)
        partes = franchise_store.split('-')

        if len(partes) == 2:
            return partes[0][3:5] + partes[1][1:]

        return franchise_store

    except Exception:
        return str(franchise_store)


def generar_txt_por_tienda(df, directorio_salida, fecha_transaccion, tienda_objetivo=None):
    try:
        for tienda, grupo in df.groupby('franchisestore'):
            tienda_formateada = formatear_numero_franquicia(str(tienda))
            if tienda_objetivo and tienda_formateada != tienda_objetivo:
                continue

            encabezado = ['H', tienda_formateada, fecha_transaccion, 'POS']
            filas_procesadas = [encabezado]

            for _, fila in grupo.iterrows():
                detalle = [
                    'D',
                    '1',
                    fila['datetimeplaced'],
                    fila['itemid'],
                    fila['quantity'],
                    fila['precio_individual'],
                    fila['netamount']
                ]

                filas_procesadas.append(detalle)

            nombre_archivo = f"001{tienda_formateada}mix{fecha_transaccion.replace('/', '')}.txt"
            archivo_txt = os.path.join(directorio_salida, nombre_archivo)

            with open(archivo_txt, 'w', encoding='utf-8') as f:
                for fila in filas_procesadas:
                    f.write('\t'.join(map(str, fila)) + '\n')

            print(f"✅ TXT generado: {archivo_txt}")

    except Exception as e:
        print(f"❌ Error al generar archivos TXT: {e}")


def extraer_zip_seguro(archivo_zip, directorio_temp):
    try:
        if not esperar_archivo_completo(archivo_zip):
            print(f"⚠️ ZIP incompleto o aún en descarga: {archivo_zip}")
            return False

        if os.path.getsize(archivo_zip) < 100:
            print(f"⚠️ ZIP vacío o demasiado pequeño: {archivo_zip}")
            return False

        with zipfile.ZipFile(archivo_zip, 'r') as zip_ref:
            zip_ref.extractall(directorio_temp)

        return True

    except zipfile.BadZipFile:
        print(f"❌ ZIP dañado o incompleto: {archivo_zip}")
        return False

    except Exception as e:
        print(f"❌ Error extrayendo ZIP {archivo_zip}: {e}")
        return False


def limpiar_solo_mix_en_plano(directorio_plano):
    print("\n🧹 Limpiando únicamente archivos MIX en carpeta plana...")

    patron = os.path.join(directorio_plano, "*mix*.txt")

    for archivo in glob.glob(patron):
        try:
            os.remove(archivo)
            print(f"🗑️ Eliminado MIX anterior: {os.path.basename(archivo)}")
        except Exception as e:
            print(f"⚠️ No se pudo eliminar {archivo}: {e}")


def procesar_archivos_grandes(directorio_reportes, directorio_plano, tienda_objetivo=None, modo_especial=False):
    print("\n===== PROCESAMIENTO DE ARCHIVOS MIX =====")
    print(f"Directorio reportes: {directorio_reportes}")
    print(f"Directorio plano: {directorio_plano}")

    directorio_temp = os.path.join(directorio_reportes, "temp_proceso")
    directorio_salida = os.path.join(directorio_reportes, "Archivos Mix")

    if os.path.exists(directorio_temp):
        shutil.rmtree(directorio_temp)

    os.makedirs(directorio_temp, exist_ok=True)
    os.makedirs(directorio_salida, exist_ok=True)
    os.makedirs(directorio_plano, exist_ok=True)

    archivos_zip = sorted(
        glob.glob(os.path.join(directorio_reportes, "*.zip")),
        key=os.path.getmtime
    )

    if not archivos_zip:
        print(f"❌ No se encontraron archivos ZIP en {directorio_reportes}")
        return None

    archivos_generados = 0

    for archivo_zip in archivos_zip:
        nombre_zip = os.path.basename(archivo_zip)
        print(f"\n📦 Procesando ZIP: {nombre_zip}")

        if os.path.exists(directorio_temp):
            shutil.rmtree(directorio_temp)

        os.makedirs(directorio_temp, exist_ok=True)

        if not extraer_zip_seguro(archivo_zip, directorio_temp):
            continue

        fecha_zip = transformar_fecha(nombre_zip.split('.')[0][-10:])
        todos_archivos = glob.glob(os.path.join(directorio_temp, "**"), recursive=True)

        for archivo in todos_archivos:
            if archivo.endswith(('.xlsx', '.xls', '.csv')):
                print(f"  🔎 Analizando archivo: {os.path.basename(archivo)}")

                try:
                    if archivo.endswith(('.xlsx', '.xls')):
                        df = pd.read_excel(archivo)
                    else:
                        df = pd.read_csv(archivo)

                    df.columns = [col.lower().strip() for col in df.columns]

                    if "summary-items" not in os.path.basename(archivo).lower():
                        continue

                    rename_map = {}

                    if "quantity" in df.columns and "itemquantity" not in df.columns:
                        rename_map["quantity"] = "itemquantity"

                    if "netamount" in df.columns and "royaltyobligation" not in df.columns:
                        rename_map["netamount"] = "royaltyobligation"

                    if rename_map:
                        df = df.rename(columns=rename_map)

                    columnas_necesarias = [
                        "franchisestore",
                        "itemid",
                        "itemquantity",
                        "royaltyobligation"
                    ]

                    faltantes = [c for c in columnas_necesarias if c not in df.columns]

                    if faltantes:
                        print(f"  ❌ Faltan columnas en {os.path.basename(archivo)}: {faltantes}")
                        continue

                    df["datetimeplaced"] = "12:00"
                    df = df[df["itemid"].astype(str).str.strip() != "-1"]

                    qty = to_numeric_clean(df["itemquantity"]).fillna(0)
                    net = to_numeric_clean(df["royaltyobligation"]).fillna(0)

                    df["quantity"] = qty
                    df["netamount"] = net

                    with np.errstate(divide='ignore', invalid='ignore'):
                        precio_unit = np.where(qty > 0, net / qty, 0.0)

                    df["precio_individual"] = np.round(precio_unit.astype(float), 2)

                    n_bad_qty = int((qty == 0).sum())
                    n_bad_net = int((net == 0).sum())

                    if n_bad_qty or n_bad_net:
                        print(f"  ℹ️ Limpiezas: qty_no_numerico/0={n_bad_qty}, net_no_numerico/0={n_bad_net}")

                    generar_txt_por_tienda(df, directorio_salida, fecha_zip, tienda_objetivo)
                    archivos_generados += 1

                except Exception as e:
                    print(f"  ❌ Error al procesar {os.path.basename(archivo)}: {e}")

    if os.path.exists(directorio_temp):
        shutil.rmtree(directorio_temp)

    if archivos_generados == 0:
        print("⚠️ No se generaron archivos MIX.")
        return None


    if not modo_especial:
        limpiar_solo_mix_en_plano(directorio_plano)

    print("\n📤 Copiando archivos MIX al plano...")

    patron = f"*mix{fecha_zip.replace('/', '')}.txt"
    for archivo in glob.glob(os.path.join(directorio_salida, patron)):
        if tienda_objetivo and not os.path.basename(archivo).startswith(f"001{tienda_objetivo}mix"):
            continue
        destino = os.path.join(directorio_plano, os.path.basename(archivo))
        shutil.copy(archivo, destino)
        print(f"✅ Copiado: {os.path.basename(archivo)}")

    print("\n✅ PROCESAMIENTO MIX COMPLETADO")
    print(f"📁 Archivos guardados en: {directorio_salida}")
    print(f"📁 Copias también en: {directorio_plano}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date")
    parser.add_argument("--store")
    parser.add_argument("--special", action="store_true")
    args = parser.parse_args()
    directorio_reportes = (
        os.path.join(DIRECTORIO_BASE, args.report_date)
        if args.report_date else obtener_ultima_carpeta_fecha(DIRECTORIO_BASE)
    )

    if directorio_reportes:
        procesar_archivos_grandes(directorio_reportes, DIRECTORIO_PLANO, args.store, args.special)
    else:
        print("⚠️ No se pudo determinar un directorio de reportes válido.")
