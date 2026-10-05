import os
import zipfile
import pandas as pd
import glob
import shutil
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


def formatear_numero_franquicia(franchise_store):
    try:
        franchise_store = str(franchise_store)
        partes = franchise_store.split('-')
        if len(partes) == 2:
            return partes[0][3:] + partes[1][1:]
        return franchise_store
    except Exception:
        return str(franchise_store)


def transformar_fecha(fecha):
    try:
        fecha = str(fecha)
        partes = fecha.split('-')
        if len(partes) == 3:
            return f"{partes[1]}/{partes[2]}/{partes[0]}"
        return fecha
    except Exception:
        return str(fecha)


def extraer_hora(datetime_value):
    try:
        datetime_value = str(datetime_value)
        if 'T' in datetime_value:
            return datetime_value.split('T')[1].split('.')[0]
        elif ' ' in datetime_value:
            return datetime_value.split(' ')[1].split('.')[0]
        return datetime_value
    except Exception:
        return str(datetime_value)


def validar_columnas_requeridas(df, columnas, nombre_archivo):
    columnas_faltantes = [col for col in columnas if col not in df.columns]
    if columnas_faltantes:
        print(f"❌ El archivo {nombre_archivo} no contiene columnas necesarias: {', '.join(columnas_faltantes)}. Será omitido.")
        return False
    return True


def extraer_y_procesar_archivos_zip(archivo_zip, directorio_temp):
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
        print(f"❌ Error extrayendo {archivo_zip}: {e}")
        return False


def procesar_cabecera(df, franchise_store, business_date):
    df = df.copy()

    if 'datetimeplaced' not in df.columns:
        df['datetimeplaced'] = ""

    df.loc[:, 'hora'] = df['datetimeplaced'].astype(str).apply(extraer_hora)

    clientes_totales = len(df)
    clientes_medio_dia = len(df[df['hora'] < "12:00:00"])
    clientes_comida_cena = len(df[df['hora'] >= "12:00:00"])

    ventas_totales = pd.to_numeric(
        df['royaltyobligation'],
        errors='coerce'
    ).fillna(0).sum()

    promedio_ventas = round(ventas_totales / clientes_totales, 2) if clientes_totales > 0 else 0.00

    return [
        'H',
        franchise_store,
        '001',
        business_date,
        'POS',
        '0.00',
        '0.00',
        '0.00',
        '0',
        '0',
        '0.00',
        str(clientes_medio_dia),
        str(clientes_comida_cena),
        str(clientes_totales),
        f"{promedio_ventas:.2f}"
    ]


def procesar_detalle(df, business_date):
    ventas_totales = pd.to_numeric(
        df['royaltyobligation'],
        errors='coerce'
    ).fillna(0).sum() if 'royaltyobligation' in df.columns else 0.00

    impuestos_totales = pd.to_numeric(
        df['salestax'],
        errors='coerce'
    ).fillna(0).sum() if 'salestax' in df.columns else 0.00

    descuentos_totales = 0.00

    return [
        ['D', '2', '0', '0', '000085', business_date, f"{round(ventas_totales, 2):.2f}", business_date],
        ['D', '2', '1', '0', '000077', business_date, f"{round(impuestos_totales, 2):.2f}", business_date],
        ['D', '2', '6', '0', '000089', business_date, f"{round(descuentos_totales, 2):.2f}", business_date]
    ]


def guardar_archivo_txt(franchise_store, registros, directorio_salida, fecha_zip):
    try:
        fecha_zip_formateada = transformar_fecha(fecha_zip)
        nombre_txt = f"002{franchise_store}vtas{fecha_zip_formateada.replace('/', '')}.txt"
        archivo_txt = os.path.join(directorio_salida, nombre_txt)

        with open(archivo_txt, 'w', encoding='utf-8') as file:
            for registro in registros:
                file.write('\t'.join(map(str, registro)) + '\n')

        print(f"✅ Archivo TXT guardado: {archivo_txt}")

    except Exception as e:
        print(f"❌ Error al guardar TXT para tienda {franchise_store}: {e}")


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
        print(f"❌ Error al identificar última carpeta por fecha: {e}")
        return None


def limpiar_solo_ventas_en_plano(directorio_plano):
    print("\n🧹 Limpiando únicamente archivos VENTAS en carpeta plana...")

    patron = os.path.join(directorio_plano, "*vtas*.txt")

    for archivo in glob.glob(patron):
        try:
            os.remove(archivo)
            print(f"🗑️ Eliminado venta anterior: {os.path.basename(archivo)}")
        except Exception as e:
            print(f"⚠️ No se pudo eliminar {archivo}: {e}")


def procesar_archivos_grandes(directorio_reportes, directorio_plano, tienda_objetivo=None, modo_especial=False):
    print("\n===== PROCESAMIENTO DE ARCHIVOS VENTAS CRUNCH =====")
    print(f"Directorio reportes: {directorio_reportes}")
    print(f"Directorio plano: {directorio_plano}")

    directorio_temp = os.path.join(directorio_reportes, "temp_proceso")
    directorio_salida = os.path.join(directorio_reportes, "Archivos de venta de Crunch")

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

    registros_por_tienda = {}
    fecha_zip_final = None

    for archivo_zip in archivos_zip:
        nombre_zip = os.path.basename(archivo_zip)
        print(f"\n📦 Procesando ZIP: {nombre_zip}")

        if os.path.exists(directorio_temp):
            shutil.rmtree(directorio_temp)

        os.makedirs(directorio_temp, exist_ok=True)

        if not extraer_y_procesar_archivos_zip(archivo_zip, directorio_temp):
            continue

        fecha_zip = nombre_zip.split('.')[0][-10:]
        fecha_zip_final = fecha_zip

        todos_archivos = glob.glob(os.path.join(directorio_temp, "**"), recursive=True)

        for archivo in todos_archivos:
            if archivo.endswith(('.xlsx', '.xls', '.csv')):
                try:
                    nombre_archivo = os.path.basename(archivo)
                    print(f"  🔎 Analizando archivo: {nombre_archivo}")

                    if archivo.endswith(('.xlsx', '.xls')):
                        df = pd.read_excel(archivo)
                    else:
                        df = pd.read_csv(archivo)

                    df.columns = [col.lower().strip() for col in df.columns]

                    if "detail-orders" not in nombre_archivo.lower():
                        continue

                    if not validar_columnas_requeridas(
                        df,
                        ['franchisestore', 'businessdate'],
                        nombre_archivo
                    ):
                        continue

                    if 'royaltyobligation' in df.columns:
                        df['royaltyobligation'] = pd.to_numeric(
                            df['royaltyobligation'],
                            errors='coerce'
                        ).fillna(0)

                    if 'salestax' in df.columns:
                        df['salestax'] = pd.to_numeric(
                            df['salestax'],
                            errors='coerce'
                        ).fillna(0)

                    for tienda in df['franchisestore'].astype(str).unique():
                        tienda_formateada = formatear_numero_franquicia(tienda)
                        if tienda_objetivo and tienda_formateada != tienda_objetivo:
                            continue
                        df_tienda = df[df['franchisestore'].astype(str) == tienda].copy()

                        business_date = transformar_fecha(str(df_tienda['businessdate'].iloc[0]))

                        registro_cabecera = procesar_cabecera(
                            df_tienda,
                            tienda_formateada,
                            business_date
                        )

                        detalles = procesar_detalle(df_tienda, business_date)

                        if tienda_formateada not in registros_por_tienda:
                            registros_por_tienda[tienda_formateada] = []

                        registros_por_tienda[tienda_formateada].append(registro_cabecera)
                        registros_por_tienda[tienda_formateada].extend(detalles)

                except Exception as e:
                    print(f"❌ Error procesando {archivo}: {type(e).__name__} - {e}")

    if os.path.exists(directorio_temp):
        shutil.rmtree(directorio_temp)

    if not registros_por_tienda:
        print("⚠️ No se generaron registros de ventas.")
        return None

    for tienda, registros in registros_por_tienda.items():
        guardar_archivo_txt(tienda, registros, directorio_salida, fecha_zip_final)

    if not modo_especial:
        limpiar_solo_ventas_en_plano(directorio_plano)

    print("\n📤 Copiando archivos VENTAS al plano...")

    fecha_archivo = transformar_fecha(fecha_zip_final).replace('/', '')
    patron = f"*vtas{fecha_archivo}.txt"
    for archivo in glob.glob(os.path.join(directorio_salida, patron)):
        if tienda_objetivo and not os.path.basename(archivo).startswith(f"002{tienda_objetivo}vtas"):
            continue
        destino = os.path.join(directorio_plano, os.path.basename(archivo))
        shutil.copy(archivo, destino)
        print(f"✅ Copiado: {os.path.basename(archivo)}")

    print("\n✅ PROCESAMIENTO DE VENTAS COMPLETADO")
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
