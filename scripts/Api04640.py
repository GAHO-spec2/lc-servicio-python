import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.parse
import uuid
import zipfile
import argparse
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# LCE DOWNLOADER 04640 - V2
# ============================================================
# Mejoras principales:
# - Espera inteligente del reporte (polling)
# - Reintentos HTTP con backoff
# - Timeouts explícitos
# - Descarga real por streaming
# - Archivo temporal .part
# - Validación Content-Length
# - Validación profunda del ZIP
# - No publica archivos incompletos
# - No imprime URL SAS
# - Idempotencia: si el ZIP correcto ya existe, no redescarga
# - Estado JSON + log dedicado
# - Códigos de salida específicos para el BAT maestro
# ============================================================

AUTH_URL = "https://portalapi.lcegateway.com/Token"
METADATA_URL = (
    "https://portalapi.lcegateway.com/"
    "GetReportBlobs?userName=4640DNAVARRO&fileName=all"
)

# ------------------------------------------------------------
# CREDENCIALES
# Copia aquí las mismas credenciales que utilizabas en Api04640.py
# ------------------------------------------------------------
USUARIO = os.environ.get("LC_04640_USERNAME", "")
PASSWORD = os.environ.get("LC_04640_PASSWORD", "")
HMAC_USER = os.environ.get("LC_04640_HMAC_USER", "")
HMAC_KEY = os.environ.get("LC_04640_HMAC_KEY", "")

# ------------------------------------------------------------
# CONFIGURACIÓN DE FRANQUICIA
# ------------------------------------------------------------
FRANQUICIA = "04640"
REPORT_PREFIX = "04640"
CARPETA_BASE = Path(os.environ.get("LC_04640_REPORTS_DIR", str(Path(__file__).resolve().parent / "datos" / "04640")))

# Tiempo máximo durante el cual se esperará que aparezca el reporte.
MAX_WAIT_MINUTES = int(os.getenv("LCE_04640_MAX_WAIT_MINUTES", "90"))

# Cada cuánto volver a consultar metadata si el reporte aún no existe.
POLL_INTERVAL_SECONDS = int(os.getenv("LCE_04640_POLL_SECONDS", "300"))

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 180
CHUNK_SIZE = 1024 * 1024  # 1 MB
MIN_ZIP_SIZE = 100

LOG_FILE = CARPETA_BASE / "descarga_04640.log"
STATE_FILE = CARPETA_BASE / "estado_descarga_04640.json"

# ------------------------------------------------------------
# CÓDIGOS DE SALIDA
# ------------------------------------------------------------
EXIT_OK = 0
EXIT_REPORT_NOT_AVAILABLE = 10
EXIT_AUTH = 20
EXIT_METADATA = 30
EXIT_DOWNLOAD = 40
EXIT_VALIDATION = 50
EXIT_CONFIG = 60
EXIT_UNEXPECTED = 99


class DownloaderError(Exception):
    def __init__(self, message, exit_code):
        super().__init__(message)
        self.exit_code = exit_code


def preparar_logging():
    CARPETA_BASE.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("lce04640")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = preparar_logging()


def guardar_estado(**datos):
    payload = {
        "franquicia": FRANQUICIA,
        "actualizado": dt.datetime.now().isoformat(timespec="seconds"),
        **datos,
    }

    temp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, STATE_FILE)


def crear_session():
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def validar_configuracion():
    faltantes = []

    if not USUARIO.strip():
        faltantes.append("USUARIO")
    if not PASSWORD.strip():
        faltantes.append("PASSWORD")
    if not HMAC_USER.strip():
        faltantes.append("HMAC_USER")
    if not HMAC_KEY.strip():
        faltantes.append("HMAC_KEY")

    if faltantes:
        raise DownloaderError(
            "Faltan datos de configuración: " + ", ".join(faltantes),
            EXIT_CONFIG,
        )

    try:
        base64.b64decode(HMAC_KEY.strip(), validate=True)
    except Exception as exc:
        raise DownloaderError(
            "HMAC_KEY no parece tener formato Base64 válido.",
            EXIT_CONFIG,
        ) from exc


def generar_hmac_header(http_method, request_url, request_body, app_id, api_key_b64):
    url_enc = urllib.parse.quote(request_url, safe="").lower()
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_hash = ""

    if http_method.upper() != "GET" and request_body:
        md5_digest = hashlib.md5(request_body.encode("utf-8")).digest()
        body_hash = base64.b64encode(md5_digest).decode()

    raw_sig = (
        f"{app_id}{http_method.upper()}{url_enc}{timestamp}{nonce}{body_hash}"
    ).encode("utf-8")

    key_bytes = base64.b64decode(api_key_b64)
    sig_digest = hmac.new(key_bytes, raw_sig, hashlib.sha256).digest()
    sig_b64 = base64.b64encode(sig_digest).decode()

    return f"amx {app_id}:{sig_b64}:{nonce}:{timestamp}"


def autenticar(session):
    logger.info("AUTH | Solicitando token...")

    try:
        response = session.post(
            AUTH_URL,
            data={
                "username": USUARIO,
                "password": PASSWORD,
                "grant_type": "password",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except requests.RequestException as exc:
        raise DownloaderError(
            f"AUTH | Error de conexión: {exc}",
            EXIT_AUTH,
        ) from exc

    if response.status_code >= 400:
        raise DownloaderError(
            f"AUTH | HTTP {response.status_code} al solicitar token.",
            EXIT_AUTH,
        )

    try:
        token = response.json().get("access_token")
    except Exception as exc:
        raise DownloaderError(
            "AUTH | La respuesta no contiene JSON válido.",
            EXIT_AUTH,
        ) from exc

    if not token:
        raise DownloaderError(
            "AUTH | No se recibió access_token.",
            EXIT_AUTH,
        )

    logger.info("AUTH | Token obtenido correctamente.")
    return token


def consultar_metadata(session, token, nombre_zip):
    hmac_header = generar_hmac_header(
        "GET",
        METADATA_URL,
        "",
        HMAC_USER,
        HMAC_KEY,
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Hmac_User": HMAC_USER,
        "Hmac_Key": HMAC_KEY,
        "HMacAuthorizationHeader": hmac_header,
        "usuario": USUARIO,
        "nombreArchivo": nombre_zip,
    }

    try:
        response = session.get(
            METADATA_URL,
            headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except requests.RequestException as exc:
        raise DownloaderError(
            f"METADATA | Error de conexión: {exc}",
            EXIT_METADATA,
        ) from exc

    # Indicamos al caller que renueve token.
    if response.status_code == 401:
        return None, True

    if response.status_code >= 400:
        raise DownloaderError(
            f"METADATA | HTTP {response.status_code}.",
            EXIT_METADATA,
        )

    try:
        reports = response.json()
        if isinstance(reports, str):
            reports = json.loads(reports)
    except Exception as exc:
        raise DownloaderError(
            "METADATA | La respuesta no contiene JSON válido.",
            EXIT_METADATA,
        ) from exc

    if not isinstance(reports, list):
        raise DownloaderError(
            "METADATA | Formato inesperado: se esperaba una lista de reportes.",
            EXIT_METADATA,
        )

    entry = next(
        (r for r in reports if r.get("ReportName") == nombre_zip),
        None,
    )

    if not entry:
        return None, False

    blob_uri = entry.get("ReportBlobUri")
    if not blob_uri:
        raise DownloaderError(
            "METADATA | El reporte fue encontrado pero no contiene ReportBlobUri.",
            EXIT_METADATA,
        )

    return blob_uri, False


def descargar_blob(session, blob_uri, ruta_part):
    logger.info("DOWNLOAD | Iniciando descarga...")

    try:
        with session.get(
            blob_uri,
            stream=True,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        ) as response:
            if response.status_code >= 400:
                raise DownloaderError(
                    f"DOWNLOAD | HTTP {response.status_code}.",
                    EXIT_DOWNLOAD,
                )

            content_type = response.headers.get("Content-Type", "desconocido")
            content_length_header = response.headers.get("Content-Length")

            expected_size = None
            if content_length_header:
                try:
                    expected_size = int(content_length_header)
                except ValueError:
                    expected_size = None

            logger.info("DOWNLOAD | Content-Type: %s", content_type)
            if expected_size is not None:
                logger.info("DOWNLOAD | Tamaño esperado: %s bytes", expected_size)

            total = 0
            with open(ruta_part, "wb") as fh:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    total += len(chunk)

    except DownloaderError:
        raise
    except requests.RequestException as exc:
        raise DownloaderError(
            f"DOWNLOAD | Error de conexión: {exc}",
            EXIT_DOWNLOAD,
        ) from exc
    except OSError as exc:
        raise DownloaderError(
            f"DOWNLOAD | No se pudo escribir archivo temporal: {exc}",
            EXIT_DOWNLOAD,
        ) from exc

    logger.info("DOWNLOAD | Descarga completa: %s bytes", total)

    if expected_size is not None and total != expected_size:
        raise DownloaderError(
            f"DOWNLOAD | Content-Length no coincide. Esperado={expected_size}, descargado={total}.",
            EXIT_DOWNLOAD,
        )

    if total < MIN_ZIP_SIZE:
        raise DownloaderError(
            f"DOWNLOAD | Archivo demasiado pequeño: {total} bytes.",
            EXIT_DOWNLOAD,
        )

    return total


def validar_zip(ruta_zip, fecha_reporte):
    if not ruta_zip.exists():
        return False, "El archivo no existe.", None

    try:
        size = ruta_zip.stat().st_size
    except OSError as exc:
        return False, f"No fue posible consultar tamaño: {exc}", None

    if size < MIN_ZIP_SIZE:
        return False, f"Archivo demasiado pequeño: {size} bytes.", None

    try:
        with ruta_zip.open("rb") as fh:
            signature = fh.read(2)
    except OSError as exc:
        return False, f"No fue posible leer firma: {exc}", None

    if signature != b"PK":
        return False, "El archivo no comienza con firma PK; no es un ZIP válido.", None

    if not zipfile.is_zipfile(ruta_zip):
        return False, "zipfile.is_zipfile devolvió False.", None

    try:
        with zipfile.ZipFile(ruta_zip, "r") as zf:
            names = [name for name in zf.namelist() if not name.endswith("/")]

            if not names:
                return False, "El ZIP está vacío.", None

            bad = zf.testzip()
            if bad:
                return False, f"Archivo interno dañado: {bad}", None

            csv_names = [name for name in names if name.lower().endswith(".csv")]
            if not csv_names:
                return False, "El ZIP no contiene archivos CSV.", None

            # Validación defensiva de fecha: al menos un CSV debe corresponder
            # al día solicitado. Esto evita aceptar un ZIP de otra fecha.
            if not any(fecha_reporte in Path(name).name for name in csv_names):
                return (
                    False,
                    f"Ningún CSV interno contiene la fecha esperada {fecha_reporte}.",
                    None,
                )

            details = {
                "files": len(names),
                "csv": len(csv_names),
                "size": size,
            }

            return (
                True,
                f"ZIP válido: {len(names)} archivos, {len(csv_names)} CSV.",
                details,
            )

    except (zipfile.BadZipFile, OSError) as exc:
        return False, f"Error validando ZIP: {exc}", None


def archivar_invalido(ruta):
    if not ruta.exists():
        return None

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    destino = ruta.with_name(f"{ruta.name}.invalid_{stamp}")

    try:
        os.replace(ruta, destino)
        logger.warning("VALIDATION | Archivo inválido preservado como: %s", destino)
        return destino
    except OSError as exc:
        logger.warning("VALIDATION | No fue posible archivar inválido: %s", exc)
        try:
            ruta.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def main():
    validar_configuracion()

    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date", help="Fecha especial YYYY-MM-DD")
    args = parser.parse_args()

    if args.report_date:
        try:
            fecha_reporte = dt.datetime.strptime(
                args.report_date,
                "%Y-%m-%d",
            ).strftime("%Y-%m-%d")
        except ValueError:
            logger.error("CONFIG | La fecha debe tener formato YYYY-MM-DD.")
            sys.exit(EXIT_CONFIG)
    else:
        fecha_reporte = (dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    nombre_zip = f"{REPORT_PREFIX}_{fecha_reporte}.zip"
    carpeta_destino = CARPETA_BASE / fecha_reporte
    ruta_zip = carpeta_destino / nombre_zip
    ruta_part = carpeta_destino / f"{nombre_zip}.part"

    logger.info("=" * 70)
    logger.info("INICIO LCE DOWNLOADER 04640 V2")
    logger.info("Fecha reporte: %s", fecha_reporte)
    logger.info("Archivo esperado: %s", nombre_zip)
    logger.info("Ventana máxima: %s minutos", MAX_WAIT_MINUTES)
    logger.info("Intervalo consulta: %s segundos", POLL_INTERVAL_SECONDS)
    logger.info("=" * 70)

    guardar_estado(
        estado="INICIANDO",
        fecha_reporte=fecha_reporte,
        archivo=nombre_zip,
        codigo=None,
    )

    carpeta_destino.mkdir(parents=True, exist_ok=True)

    # Idempotencia: si ya existe y es válido, no hacemos llamadas innecesarias.
    if ruta_zip.exists():
        ok, msg, details = validar_zip(ruta_zip, fecha_reporte)
        if ok:
            logger.info("CACHE | El ZIP ya existe y pasó todas las validaciones.")
            logger.info("CACHE | %s", msg)
            guardar_estado(
                estado="SUCCESS_CACHE",
                fecha_reporte=fecha_reporte,
                archivo=nombre_zip,
                ruta=str(ruta_zip),
                detalles=details,
                codigo=EXIT_OK,
            )
            return EXIT_OK

        logger.warning("CACHE | ZIP existente inválido: %s", msg)
        archivar_invalido(ruta_zip)

    # Eliminar temporal viejo si quedó de una ejecución interrumpida.
    if ruta_part.exists():
        try:
            ruta_part.unlink()
            logger.info("RECOVERY | Se eliminó archivo .part anterior.")
        except OSError as exc:
            raise DownloaderError(
                f"No fue posible eliminar .part anterior: {exc}",
                EXIT_DOWNLOAD,
            ) from exc

    session = crear_session()
    token = autenticar(session)

    deadline = time.monotonic() + (MAX_WAIT_MINUTES * 60)
    intento = 0

    while True:
        intento += 1
        logger.info("METADATA | Intento %s", intento)

        blob_uri, renovar_token = consultar_metadata(session, token, nombre_zip)

        if renovar_token:
            logger.warning("METADATA | Token rechazado (401). Renovando token una vez...")
            token = autenticar(session)
            blob_uri, renovar_token = consultar_metadata(session, token, nombre_zip)

            if renovar_token:
                raise DownloaderError(
                    "METADATA | Continúa HTTP 401 después de renovar token.",
                    EXIT_METADATA,
                )

        if blob_uri:
            logger.info("METADATA | Reporte encontrado.")
            logger.info("METADATA | URL segura de descarga obtenida.")

            try:
                descargar_blob(session, blob_uri, ruta_part)

                logger.info("VALIDATION | Validando archivo descargado...")
                ok, msg, details = validar_zip(ruta_part, fecha_reporte)

                if not ok:
                    logger.error("VALIDATION | %s", msg)
                    archivar_invalido(ruta_part)

                    if time.monotonic() < deadline:
                        logger.warning(
                            "RECOVERY | La descarga no pasó validación. Se consultará metadata nuevamente."
                        )
                    else:
                        raise DownloaderError(msg, EXIT_VALIDATION)
                else:
                    logger.info("VALIDATION | %s", msg)
                    logger.info("VALIDATION | Archivos internos: %s", details["files"])

                    os.replace(ruta_part, ruta_zip)

                    logger.info("SUCCESS | ZIP publicado correctamente.")
                    logger.info("SUCCESS | Ruta: %s", ruta_zip)
                    logger.info("SUCCESS | Tamaño: %s bytes", details["size"])
                    logger.info("SUCCESS | Intentos: %s", intento)

                    guardar_estado(
                        estado="SUCCESS",
                        fecha_reporte=fecha_reporte,
                        archivo=nombre_zip,
                        ruta=str(ruta_zip),
                        intentos=intento,
                        detalles=details,
                        codigo=EXIT_OK,
                    )
                    return EXIT_OK

            except DownloaderError as exc:
                if ruta_part.exists():
                    archivar_invalido(ruta_part)

                # Errores de descarga/validación pueden ser temporales si aún
                # estamos dentro de la ventana; volvemos a pedir metadata para
                # obtener una SAS nueva.
                if exc.exit_code in (EXIT_DOWNLOAD, EXIT_VALIDATION) and time.monotonic() < deadline:
                    logger.warning("RECOVERY | %s", exc)
                    logger.warning("RECOVERY | Se volverá a intentar mientras quede ventana disponible.")
                else:
                    raise

        else:
            logger.info("METADATA | El reporte todavía no está disponible.")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloaderError(
                f"El reporte {nombre_zip} no estuvo disponible dentro de la ventana de {MAX_WAIT_MINUTES} minutos.",
                EXIT_REPORT_NOT_AVAILABLE,
            )

        wait_seconds = min(POLL_INTERVAL_SECONDS, max(1, int(remaining)))
        logger.info("WAIT | Próxima consulta en %s segundos.", wait_seconds)
        guardar_estado(
            estado="WAITING_REPORT",
            fecha_reporte=fecha_reporte,
            archivo=nombre_zip,
            intentos=intento,
            siguiente_consulta_segundos=wait_seconds,
            codigo=None,
        )
        time.sleep(wait_seconds)


if __name__ == "__main__":
    exit_code = EXIT_UNEXPECTED

    try:
        exit_code = main()

    except DownloaderError as exc:
        exit_code = exc.exit_code
        logger.error("ERROR | %s", exc)

        try:
            guardar_estado(
                estado="ERROR",
                error=str(exc),
                codigo=exit_code,
            )
        except Exception:
            pass

    except KeyboardInterrupt:
        exit_code = EXIT_UNEXPECTED
        logger.error("ERROR | Ejecución interrumpida manualmente.")

        try:
            guardar_estado(
                estado="INTERRUPTED",
                error="Ejecución interrumpida manualmente",
                codigo=exit_code,
            )
        except Exception:
            pass

    except Exception as exc:
        exit_code = EXIT_UNEXPECTED
        logger.exception("ERROR INESPERADO | %s", exc)

        try:
            guardar_estado(
                estado="ERROR_UNEXPECTED",
                error=str(exc),
                codigo=exit_code,
            )
        except Exception:
            pass

    logger.info("END | Código de salida: %s", exit_code)
    sys.exit(exit_code)
