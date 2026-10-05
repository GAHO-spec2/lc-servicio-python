import os
import zipfile
import pandas as pd
import glob
import shutil
import argparse
from datetime import datetime

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

def procesar_archivos_grandes(directorio_reportes, directorio_plano, tienda_objetivo=None):
    print(f"\n===== PROCESAMIENTO DE ARCHIVOS =====")
    print(f"Directorio: {directorio_reportes}")

    mapeo_campos = {
        "FranchiseStore": ["franchisestore", "franchise_store", "franchise store", "store", "tienda"],
        "DateTimePlaced": ["datetimeplaced", "datetime_placed", "datetime placed", "placed date", "order date"],
        "ItemId": ["itemid", "item_id", "item id", "id de artículo"],
        "Quantity": ["quantity", "cantidad", "qty"],
        "NetAmount": ["netamount", "net_amount", "net amount", "monto neto"],
    }

    directorio_temp = os.path.join(directorio_reportes, "temp_proceso")
    if os.path.exists(directorio_temp):
        shutil.rmtree(directorio_temp)
    os.makedirs(directorio_temp)

    directorio_salida = os.path.join(directorio_reportes, "Archivos Mix")
    if not os.path.exists(directorio_salida):
        os.makedirs(directorio_salida)

    if not os.path.exists(directorio_plano):
        os.makedirs(directorio_plano)

    archivos_zip = glob.glob(os.path.join(directorio_reportes, "*.zip"))
    if not archivos_zip:
        print(f"❌ No se encontraron archivos ZIP en {directorio_reportes}")
        return None

    for archivo_zip in archivos_zip:
        nombre_zip = os.path.basename(archivo_zip)
        print(f"\nProcesando ZIP: {nombre_zip}")

        try:
            with zipfile.ZipFile(archivo_zip, 'r') as zip_ref:
                zip_ref.extractall(directorio_temp)

            fecha_zip = transformar_fecha(nombre_zip.split('.')[0][-10:])

            todos_archivos = glob.glob(os.path.join(directorio_temp, "**"), recursive=True)

            for archivo in todos_archivos:
                if archivo.endswith(('.xlsx', '.xls', '.csv')):
                    print(f"  Analizando archivo: {os.path.basename(archivo)}")
                    try:
                        if archivo.endswith(('.xlsx', '.xls')):
                            df = pd.read_excel(archivo)
                        elif archivo.endswith('.csv'):
                            df = pd.read_csv(archivo)

                        df.columns = [col.lower().strip() for col in df.columns]

                        if "Detail-OrderLines" in os.path.basename(archivo):
                            if 'netamount' in df.columns and 'quantity' in df.columns:
                                df['precio_individual'] = round(df['netamount'] / df['quantity'], 2)

                            if 'itemid' in df.columns:
                                df = df[df['itemid'] != -1]

                            if 'datetimeplaced' in df.columns:
                                df['datetimeplaced'] = df['datetimeplaced'].astype(str).apply(extraer_hora_minuto)

                            generar_txt_por_tienda(
                                df, directorio_salida, fecha_zip, directorio_plano, tienda_objetivo
                            )
                    except Exception as e:
                        print(f"  ❌ Error al procesar {os.path.basename(archivo)}: {e}")

        except Exception as e:
            print(f"❌ Error procesando ZIP {nombre_zip}: {e}")

    shutil.rmtree(directorio_temp)
    print(f"\n===== PROCESAMIENTO COMPLETADO =====")
    print(f"Archivos guardados en: {directorio_salida}")
    print(f"Copias también en: {directorio_plano}")

def transformar_fecha(fecha):
    try:
        partes = fecha.split('-')
        if len(partes) == 3:
            return f"{partes[1]}/{partes[2]}/{partes[0]}"
        return fecha
    except Exception:
        return fecha

def formatear_numero_franquicia(franchise_store):
    try:
        partes = franchise_store.split('-')
        if len(partes) == 2:
            return partes[0][3:5] + partes[1][1:]
        return franchise_store
    except Exception:
        return franchise_store

def extraer_hora_minuto(datetime_value):
    try:
        if "T" in datetime_value:
            tiempo = datetime_value.split('T')[1].split(':')
            return f"{tiempo[0]}:{tiempo[1]}"
        elif " " in datetime_value:
            tiempo = datetime_value.split(' ')[1].split(':')
            return f"{tiempo[0]}:{tiempo[1]}"
        return datetime_value
    except Exception:
        return datetime_value

def generar_txt_por_tienda(df, directorio_salida, fecha_transaccion, directorio_plano, tienda_objetivo=None):
    try:
        for tienda, grupo in df.groupby('franchisestore'):
            tienda_formateada = formatear_numero_franquicia(tienda)
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

            with open(archivo_txt, 'w', encoding='utf-8', newline='\r\n') as f:
                for fila in filas_procesadas:
                    f.write('\t'.join(map(str, fila)) + '\n')

            # También copia el archivo al directorio plano
            archivo_txt_plano = os.path.join(directorio_plano, nombre_archivo)
            shutil.copy(archivo_txt, archivo_txt_plano)

            print(f"✅ TXT generado: {archivo_txt}")
            print(f"📁 Copia guardada en: {archivo_txt_plano}")

    except Exception as e:
        print(f"❌ Error al generar archivos TXT: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date")
    parser.add_argument("--store")
    parser.add_argument("--special", action="store_true")
    args = parser.parse_args()
    directorio_base = os.environ.get("LC_04640_REPORTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "datos", "04640"))
    directorio_plano = os.environ.get("LC_04640_OUTPUT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "salida"))

    # Obtener la última carpeta con formato de fecha automáticamente
    directorio_reportes = (
        os.path.join(directorio_base, args.report_date)
        if args.report_date else obtener_ultima_carpeta_fecha(directorio_base)
    )
    if directorio_reportes:
        procesar_archivos_grandes(directorio_reportes, directorio_plano, args.store)
    else:
        print("⚠️ No se pudo determinar un directorio de reportes válido.")
