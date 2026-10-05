import os
import zipfile
import pandas as pd
import glob
import shutil
import argparse
from datetime import datetime

def formatear_numero_franquicia(franchise_store):
    try:
        partes = franchise_store.split('-')
        if len(partes) == 2:
            nuevo_formato = partes[0][3:] + partes[1][1:]
            return nuevo_formato
        return franchise_store
    except Exception:
        return franchise_store

def transformar_fecha(fecha):
    try:
        partes = fecha.split('-')  # Suponiendo que la fecha original está en formato YYYY-MM-DD
        if len(partes) == 3:
            return f"{partes[1]}/{partes[2]}/{partes[0]}"
        return fecha
    except Exception:
        return fecha

def extraer_hora(datetime_value):
    try:
        tiempo = datetime_value.split('T')[1].split('.')[0]  # Toma solo HH:MM:SS
        return tiempo
    except Exception:
        return datetime_value

def validar_columnas_requeridas(df, columnas, nombre_archivo):
    columnas_faltantes = [col for col in columnas if col not in df.columns]
    if columnas_faltantes:
        print(f"❌ El archivo {nombre_archivo} no contiene las columnas necesarias: {', '.join(columnas_faltantes)}. Será omitido.")
        return False
    return True

def extraer_y_procesar_archivos_zip(archivo_zip, directorio_temp):
    try:
        with zipfile.ZipFile(archivo_zip, 'r') as zip_ref:
            zip_ref.extractall(directorio_temp)
        return True
    except Exception as e:
        print(f"❌ Error extrayendo {archivo_zip}: {e}")
        return False

def procesar_archivos_grandes(directorio_reportes, directorio_plano, tienda_objetivo=None):
    print(f"\n===== PROCESAMIENTO DE ARCHIVOS =====")
    print(f"Directorio: {directorio_reportes}")

    directorio_temp = os.path.join(directorio_reportes, "temp_proceso")
    if os.path.exists(directorio_temp):
        shutil.rmtree(directorio_temp)
    os.makedirs(directorio_temp)

    directorio_salida = os.path.join(directorio_reportes, "Archivos de venta de Crunch")
    if not os.path.exists(directorio_salida):
        os.makedirs(directorio_salida)

    archivos_zip = glob.glob(os.path.join(directorio_reportes, "*.zip"))
    if not archivos_zip:
        print(f"❌ No se encontraron archivos ZIP en {directorio_reportes}")
        return None

    registros_por_tienda = {}

    for archivo_zip in archivos_zip:
        nombre_zip = os.path.basename(archivo_zip)
        print(f"\nProcesando ZIP: {nombre_zip}")

        if not extraer_y_procesar_archivos_zip(archivo_zip, directorio_temp):
            continue

        fecha_zip = nombre_zip.split('.')[0][-8:]
        todos_archivos = glob.glob(os.path.join(directorio_temp, "**"), recursive=True)

        for archivo in todos_archivos:
            if archivo.endswith(('.xlsx', '.xls', '.csv')):
                try:
                    if archivo.endswith(('.xlsx', '.xls')):
                        df = pd.read_excel(archivo)
                    elif archivo.endswith('.csv'):
                        df = pd.read_csv(archivo)

                    df.columns = [col.lower().strip() for col in df.columns]

                    if not validar_columnas_requeridas(df, ['franchisestore', 'businessdate'], archivo):
                        continue

                    if "Detail-Orders" in os.path.basename(archivo):
                        for tienda in df['franchisestore'].unique():
                            tienda_formateada = formatear_numero_franquicia(tienda)
                            if tienda_objetivo and tienda_formateada != tienda_objetivo:
                                continue
                            df_tienda = df[df['franchisestore'] == tienda].copy()
                            registro_cabecera = procesar_cabecera(
                                df_tienda, tienda_formateada, transformar_fecha(df_tienda['businessdate'].iloc[0])
                            )
                            detalles = procesar_detalle(df_tienda, transformar_fecha(df_tienda['businessdate'].iloc[0]))
                            if tienda_formateada not in registros_por_tienda:
                                registros_por_tienda[tienda_formateada] = []
                            registros_por_tienda[tienda_formateada].append(registro_cabecera)
                            registros_por_tienda[tienda_formateada].extend(detalles)

                except Exception as e:
                    print(f"❌ Error procesando {archivo}: Tipo de error: {type(e).__name__}, Detalle: {e}")

    for tienda, registros in registros_por_tienda.items():
        guardar_archivo_txt(tienda, registros, directorio_salida, fecha_zip, directorio_plano)

    shutil.rmtree(directorio_temp)
    print(f"\n✅ Todos los archivos han sido procesados en: {directorio_salida}")
    print(f"✅ Copias planas también en: {directorio_plano}")

def procesar_cabecera(df, franchise_store, business_date):
    df.loc[:, 'hora'] = df['datetimeplaced'].apply(extraer_hora)
    clientes_totales = len(df)
    clientes_medio_dia = len(df[df['hora'] < "12:00:00"])
    clientes_comida_cena = len(df[df['hora'] >= "12:00:00"])
    ventas_totales = df['royaltyobligation'].sum()
    promedio_ventas = round(ventas_totales / clientes_totales, 2) if clientes_totales > 0 else 0.00

    return [
        'H', franchise_store, '001', business_date, 'POS',
        '0.00', '0.00', '0.00', 0, 0, '0.00',
        clientes_medio_dia, clientes_comida_cena, clientes_totales, promedio_ventas
    ]

def procesar_detalle(df, business_date):
    ventas_totales = df['royaltyobligation'].sum() if 'royaltyobligation' in df.columns else 0.00
    impuestos_totales = df['salestax'].sum() if 'salestax' in df.columns else 0.00
    descuentos_totales = 0.00

    detalles = [
        ['D', '2', '0', '0', '000085', business_date, round(ventas_totales, 2), business_date],
        ['D', '2', '1', '0', '000077', business_date, round(impuestos_totales, 2), business_date],
        ['D', '2', '6', '0', '000089', business_date, round(descuentos_totales, 2), business_date]
    ]
    return detalles

def guardar_archivo_txt(franchise_store, registros, directorio_salida, fecha_zip, directorio_plano):
    try:
        fecha_zip_formateada = transformar_fecha(fecha_zip)
        nombre_txt = f"002{franchise_store}vtas{fecha_zip_formateada.replace('/', '')}.txt"
        archivo_txt = os.path.join(directorio_salida, nombre_txt)

        with open(archivo_txt, 'w', encoding='utf-8', newline='\r\n') as file:
            for registro in registros:
                registro = [transformar_fecha(valor) if isinstance(valor, str) and '/' in valor else valor for valor in registro]
                file.write('\t'.join(map(str, registro)) + '\n')

        # Copiar el archivo al directorio plano
        if not os.path.exists(directorio_plano):
            os.makedirs(directorio_plano)
        ruta_plana = os.path.join(directorio_plano, nombre_txt)
        shutil.copy(archivo_txt, ruta_plana)

        print(f"✅ Archivo TXT guardado: {archivo_txt}")
        print(f"📁 También guardado en: {ruta_plana}")
    except Exception as e:
        print(f"❌ Error al guardar el archivo TXT para la tienda {franchise_store}: {e}")

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
        carpetas_fechadas.sort(key=lambda fecha: datetime.strptime(fecha, '%Y-%m-%d'), reverse=True)
        if carpetas_fechadas:
            return os.path.join(directorio_base, carpetas_fechadas[0])
        else:
            print("❌ No se encontraron carpetas con formato de fecha.")
            return None
    except Exception as e:
        print(f"❌ Error al identificar la última carpeta por fecha: {e}")
        return None

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date")
    parser.add_argument("--store")
    parser.add_argument("--special", action="store_true")
    args = parser.parse_args()
    directorio_base = os.environ.get("LC_04640_REPORTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "datos", "04640"))
    directorio_plano = os.environ.get("LC_04640_OUTPUT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "salida"))

    directorio_reportes = (
        os.path.join(directorio_base, args.report_date)
        if args.report_date else obtener_ultima_carpeta_fecha(directorio_base)
    )
    if directorio_reportes:
        procesar_archivos_grandes(directorio_reportes, directorio_plano, args.store)
    else:
        print("⚠️ No se pudo determinar un directorio de reportes válido.")
