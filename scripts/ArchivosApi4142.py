import os
import sys
import time
import json
import uuid
import hmac
import base64
import hashlib
import zipfile
import logging
import datetime as dt
import argparse
import urllib.parse
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# LCE DOWNLOADER 4142 - V2
# ============================================================
# MEJORAS:
# - Espera inteligente si el reporte todavía no está disponible.
# - Reintentos HTTP automáticos.
# - Renovación del token si recibe 401.
# - Descarga real por streaming.
# - Descarga temporal .part.
# - Validación de tamaño.
# - Validación profunda del ZIP.
# - Validación de fecha de los CSV.
# - El ZIP definitivo solo aparece si quedó correcto.
# - Log independiente de descarga.
# - Estado JSON para futuro dashboard.
# - Códigos de salida específicos para el BAT.
# ============================================================


# ============================================================
# CONFIGURACIÓN
# ============================================================

FRANQUICIA = "4142"
PREFIJO_REPORTE = "04142"

AUTH_URL = "https://portalapi.lcegateway.com/Token"

METADATA_URL = (
    "https://portalapi.lcegateway.com/"
    "GetReportBlobs?userName=41420000&fileName=all"
)


# ------------------------------------------------------------
# CREDENCIALES
# ------------------------------------------------------------
# POR AHORA puedes colocar aquí exactamente las mismas
# credenciales que ya utilizabas.
#
# Más adelante las moveremos a variables de entorno de Windows.
# ------------------------------------------------------------

USUARIO = os.environ.get("LC_4142_USERNAME", "")
PASSWORD = os.environ.get("LC_4142_PASSWORD", "")
HMAC_USER = os.environ.get("LC_4142_HMAC_USER", "")
HMAC_KEY = os.environ.get("LC_4142_HMAC_KEY", "")


# ------------------------------------------------------------
# CARPETAS
# ------------------------------------------------------------

CARPETA_BASE = Path(os.environ.get("LC_4142_REPORTS_DIR", str(Path(__file__).resolve().parent / "datos" / "4142")))

LOG_FILE = CARPETA_BASE / "descarga_4142.log"

STATUS_FILE = CARPETA_BASE / "estado_descarga_4142.json"


# ------------------------------------------------------------
# ESPERA INTELIGENTE
# ------------------------------------------------------------
# El programa puede permanecer esperando hasta 90 minutos
# a que Little Caesars publique el reporte.
#
# Cada 5 minutos vuelve a consultar.
# ------------------------------------------------------------

MAX_WAIT_MINUTES = int(os.environ.get("LC_4142_MAX_WAIT_MINUTES", "90"))

POLL_INTERVAL_SECONDS = 300


# ------------------------------------------------------------
# TIMEOUTS HTTP
# ------------------------------------------------------------

CONNECT_TIMEOUT = 15

READ_TIMEOUT = 180


# ------------------------------------------------------------
# DESCARGA
# ------------------------------------------------------------

CHUNK_SIZE = 1024 * 1024  # 1 MB

MIN_ZIP_SIZE_BYTES = 100


# ============================================================
# CÓDIGOS DE SALIDA
# ============================================================

EXIT_OK = 0

EXIT_REPORT_NOT_AVAILABLE = 10

EXIT_AUTH_ERROR = 20

EXIT_METADATA_ERROR = 30

EXIT_DOWNLOAD_ERROR = 40

EXIT_VALIDATION_ERROR = 50

EXIT_CONFIG_ERROR = 60

EXIT_UNEXPECTED_ERROR = 99


# ============================================================
# PREPARAR CARPETA BASE
# ============================================================

CARPETA_BASE.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# CONFIGURAR LOG
# ============================================================

logger = logging.getLogger("LCE4142")

logger.setLevel(logging.INFO)

logger.handlers.clear()


formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


file_handler = logging.FileHandler(
    LOG_FILE,
    encoding="utf-8"
)

file_handler.setFormatter(formatter)

logger.addHandler(file_handler)


console_handler = logging.StreamHandler(
    sys.stdout
)

console_handler.setFormatter(formatter)

logger.addHandler(console_handler)


# ============================================================
# GUARDAR ESTADO JSON
# ============================================================

def guardar_estado(
    estado,
    mensaje,
    codigo=0,
    intento=None,
    fecha_reporte=None,
    ruta_zip=None,
    tamano_bytes=None
):

    datos = {

        "franquicia": FRANQUICIA,

        "estado": estado,

        "mensaje": mensaje,

        "codigo": codigo,

        "timestamp": dt.datetime.now().isoformat(
            timespec="seconds"
        ),

        "fecha_reporte": fecha_reporte,

        "intento": intento,

        "ruta_zip": (
            str(ruta_zip)
            if ruta_zip
            else None
        ),

        "tamano_bytes": tamano_bytes
    }


    temporal = Path(
        str(STATUS_FILE) + ".tmp"
    )


    with open(
        temporal,
        "w",
        encoding="utf-8"
    ) as archivo:

        json.dump(
            datos,
            archivo,
            ensure_ascii=False,
            indent=4
        )


    os.replace(
        temporal,
        STATUS_FILE
    )


# ============================================================
# CREAR SESIÓN HTTP ROBUSTA
# ============================================================

def crear_sesion():

    estrategia = Retry(

        total=4,

        connect=4,

        read=4,

        status=4,

        backoff_factor=1.5,

        status_forcelist=[
            429,
            500,
            502,
            503,
            504
        ],

        allowed_methods=[
            "GET",
            "POST"
        ],

        respect_retry_after_header=True
    )


    adapter = HTTPAdapter(
        max_retries=estrategia
    )


    session = requests.Session()


    session.mount(
        "https://",
        adapter
    )

    session.mount(
        "http://",
        adapter
    )


    session.headers.update({

        "User-Agent":
        "LCE-Report-Automation-4142/2.0"

    })


    return session


SESSION = crear_sesion()


# ============================================================
# VALIDAR CONFIGURACIÓN
# ============================================================

def validar_configuracion():

    faltantes = []


    if not USUARIO:
        faltantes.append(
            "USUARIO"
        )


    if not PASSWORD:
        faltantes.append(
            "PASSWORD"
        )


    if not HMAC_USER:
        faltantes.append(
            "HMAC_USER"
        )


    if not HMAC_KEY:
        faltantes.append(
            "HMAC_KEY"
        )


    if faltantes:

        raise RuntimeError(

            "Faltan datos de configuración: "
            + ", ".join(faltantes)

        )


    try:

        base64.b64decode(
            HMAC_KEY,
            validate=True
        )

    except Exception:

        raise RuntimeError(
            "HMAC_KEY no parece tener "
            "formato Base64 válido."
        )


# ============================================================
# GENERAR HMAC
# ============================================================

def generar_hmac_header(
    http_method,
    request_url,
    request_body,
    app_id,
    api_key_b64
):

    url_enc = urllib.parse.quote(
        request_url,
        safe=""
    ).lower()


    timestamp = str(
        int(time.time())
    )


    nonce = uuid.uuid4().hex


    body_hash = ""


    if (
        http_method.upper() != "GET"
        and request_body
    ):

        md5_digest = hashlib.md5(

            request_body.encode(
                "utf-8"
            )

        ).digest()


        body_hash = base64.b64encode(
            md5_digest
        ).decode()


    raw_sig = (

        f"{app_id}"

        f"{http_method.upper()}"

        f"{url_enc}"

        f"{timestamp}"

        f"{nonce}"

        f"{body_hash}"

    ).encode("utf-8")


    key_bytes = base64.b64decode(
        api_key_b64
    )


    sig_digest = hmac.new(

        key_bytes,

        raw_sig,

        hashlib.sha256

    ).digest()


    sig_b64 = base64.b64encode(
        sig_digest
    ).decode()


    return (
        f"amx "
        f"{app_id}:"
        f"{sig_b64}:"
        f"{nonce}:"
        f"{timestamp}"
    )


# ============================================================
# OBTENER FECHA DE AYER
# ============================================================

def obtener_fecha_reporte(fecha_solicitada=None):

    if fecha_solicitada:

        try:

            return dt.datetime.strptime(
                fecha_solicitada,
                "%Y-%m-%d"
            ).strftime("%Y-%m-%d")

        except ValueError as error:

            raise ValueError(
                "La fecha debe tener formato YYYY-MM-DD."
            ) from error

    return (

        dt.date.today()

        - dt.timedelta(days=1)

    ).strftime(
        "%Y-%m-%d"
    )


# ============================================================
# AUTENTICACIÓN
# ============================================================

def obtener_token():

    logger.info(
        "AUTH | Solicitando token..."
    )


    try:

        response = SESSION.post(

            AUTH_URL,

            data={

                "username":
                USUARIO,

                "password":
                PASSWORD,

                "grant_type":
                "password"

            },

            headers={

                "Content-Type":
                "application/x-www-form-urlencoded"

            },

            timeout=(
                CONNECT_TIMEOUT,
                READ_TIMEOUT
            )

        )


    except requests.RequestException as error:

        raise RuntimeError(

            f"No fue posible conectar "
            f"con autenticación: {error}"

        )


    if response.status_code != 200:

        raise RuntimeError(

            f"Autenticación HTTP "
            f"{response.status_code}"

        )


    try:

        datos = response.json()

    except ValueError:

        raise RuntimeError(

            "La respuesta de autenticación "
            "no fue JSON válido."

        )


    token = datos.get(
        "access_token"
    )


    if not token:

        raise RuntimeError(

            "La API respondió pero "
            "no entregó access_token."

        )


    logger.info(
        "AUTH | Token obtenido correctamente."
    )


    return token


# ============================================================
# CONSULTAR METADATA
# ============================================================

def buscar_reporte(
    token,
    nombre_zip
):

    for intento_token in range(2):


        hmac_header = generar_hmac_header(

            "GET",

            METADATA_URL,

            "",

            HMAC_USER,

            HMAC_KEY

        )


        headers = {

            "Authorization":
            f"Bearer {token}",

            "Hmac_User":
            HMAC_USER,

            "Hmac_Key":
            HMAC_KEY,

            "HMacAuthorizationHeader":
            hmac_header,

            "usuario":
            USUARIO,

            "nombreArchivo":
            nombre_zip

        }


        try:

            response = SESSION.get(

                METADATA_URL,

                headers=headers,

                timeout=(
                    CONNECT_TIMEOUT,
                    READ_TIMEOUT
                )

            )


        except requests.RequestException as error:

            raise RuntimeError(

                f"Error consultando metadata: "
                f"{error}"

            )


        # ----------------------------------------------------
        # TOKEN EXPIRADO
        # ----------------------------------------------------

        if (
            response.status_code == 401
            and intento_token == 0
        ):

            logger.warning(

                "AUTH | Token rechazado. "
                "Solicitando uno nuevo."

            )


            token = obtener_token()


            continue


        if response.status_code != 200:

            raise RuntimeError(

                f"Metadata respondió HTTP "
                f"{response.status_code}"

            )


        try:

            reports = response.json()


            if isinstance(
                reports,
                str
            ):

                reports = json.loads(
                    reports
                )


        except Exception:

            raise RuntimeError(

                "La respuesta de metadata "
                "no es JSON válido."

            )


        if not isinstance(
            reports,
            list
        ):

            raise RuntimeError(

                "Metadata regresó un "
                "formato inesperado."

            )


        entry = next(

            (

                reporte

                for reporte in reports

                if (
                    isinstance(
                        reporte,
                        dict
                    )

                    and reporte.get(
                        "ReportName"
                    )
                    == nombre_zip

                )

            ),

            None

        )


        return entry, token


    raise RuntimeError(

        "No fue posible consultar "
        "metadata después de renovar token."

    )


# ============================================================
# DESCARGAR BLOB POR STREAMING
# ============================================================

def descargar_blob(
    blob_uri,
    ruta_part
):

    logger.info(

        "DOWNLOAD | "
        "Iniciando descarga..."

    )


    if ruta_part.exists():

        ruta_part.unlink()


    try:

        with SESSION.get(

            blob_uri,

            stream=True,

            timeout=(
                CONNECT_TIMEOUT,
                READ_TIMEOUT
            )

        ) as response:


            if response.status_code != 200:

                raise RuntimeError(

                    f"Blob respondió HTTP "
                    f"{response.status_code}"

                )


            content_length = response.headers.get(
                "Content-Length"
            )


            if (
                content_length
                and content_length.isdigit()
            ):

                tamano_esperado = int(
                    content_length
                )

            else:

                tamano_esperado = None


            content_type = response.headers.get(

                "Content-Type",

                ""

            )


            logger.info(

                "DOWNLOAD | Content-Type: %s",

                content_type
                or "no informado"

            )


            if tamano_esperado:

                logger.info(

                    "DOWNLOAD | "
                    "Tamaño esperado: "
                    "%s bytes",

                    tamano_esperado

                )


            bytes_escritos = 0


            with open(
                ruta_part,
                "wb"
            ) as archivo:


                for chunk in response.iter_content(

                    chunk_size=CHUNK_SIZE

                ):


                    if not chunk:

                        continue


                    archivo.write(
                        chunk
                    )


                    bytes_escritos += len(
                        chunk
                    )


                archivo.flush()


                os.fsync(
                    archivo.fileno()
                )


    except Exception:

        if ruta_part.exists():

            ruta_part.unlink()


        raise


    if (
        bytes_escritos
        < MIN_ZIP_SIZE_BYTES
    ):

        ruta_part.unlink(
            missing_ok=True
        )


        raise RuntimeError(

            "La descarga solamente contiene "
            f"{bytes_escritos} bytes."

        )


    if (

        tamano_esperado

        and bytes_escritos
        != tamano_esperado

    ):

        ruta_part.unlink(
            missing_ok=True
        )


        raise RuntimeError(

            "La descarga quedó incompleta. "
            f"Esperados: {tamano_esperado}. "
            f"Recibidos: {bytes_escritos}."

        )


    logger.info(

        "DOWNLOAD | "
        "Descarga completa: %s bytes",

        bytes_escritos

    )


    return bytes_escritos


# ============================================================
# VALIDAR ZIP
# ============================================================

def validar_zip(
    ruta_zip,
    fecha_reporte
):

    if not ruta_zip.exists():

        return (
            False,
            "El archivo no existe.",
            []
        )


    tamano = ruta_zip.stat().st_size


    if tamano < MIN_ZIP_SIZE_BYTES:

        return (

            False,

            f"El archivo solo contiene "
            f"{tamano} bytes.",

            []

        )


    # --------------------------------------------------------
    # VALIDAR FIRMA PK
    # --------------------------------------------------------

    try:

        with open(
            ruta_zip,
            "rb"
        ) as archivo:

            firma = archivo.read(4)


    except Exception as error:

        return (

            False,

            f"No fue posible leer "
            f"el archivo: {error}",

            []

        )


    if not firma.startswith(
        b"PK"
    ):

        return (

            False,

            "El archivo descargado "
            "no tiene firma ZIP PK.",

            []

        )


    # --------------------------------------------------------
    # VALIDAR ESTRUCTURA ZIP
    # --------------------------------------------------------

    if not zipfile.is_zipfile(
        ruta_zip
    ):

        return (

            False,

            "zipfile.is_zipfile() "
            "determinó que no es ZIP.",

            []

        )


    try:

        with zipfile.ZipFile(
            ruta_zip,
            "r"
        ) as zf:


            archivo_danado = zf.testzip()


            if archivo_danado:

                return (

                    False,

                    "Archivo corrupto dentro "
                    f"del ZIP: {archivo_danado}",

                    []

                )


            archivos = [

                nombre

                for nombre
                in zf.namelist()

                if not nombre.endswith("/")

            ]


    except Exception as error:

        return (

            False,

            f"ZIP inválido: {error}",

            []

        )


    if not archivos:

        return (

            False,

            "El ZIP está vacío.",

            []

        )


    # --------------------------------------------------------
    # VALIDAR CSV
    # --------------------------------------------------------

    csvs = [

        nombre

        for nombre
        in archivos

        if nombre.lower().endswith(
            ".csv"
        )

    ]


    if not csvs:

        return (

            False,

            "El ZIP es válido pero "
            "no contiene archivos CSV.",

            archivos

        )


    # --------------------------------------------------------
    # VALIDAR FECHA
    # --------------------------------------------------------

    csv_fecha = [

        nombre

        for nombre
        in csvs

        if fecha_reporte
        in Path(nombre).name

    ]


    if not csv_fecha:

        return (

            False,

            "Los CSV no corresponden "
            f"a la fecha {fecha_reporte}.",

            archivos

        )


    return (

        True,

        (
            f"ZIP válido: "
            f"{len(archivos)} archivos, "
            f"{len(csvs)} CSV."
        ),

        archivos

    )


# ============================================================
# ARCHIVAR ZIP ANTERIOR DAÑADO
# ============================================================

def archivar_zip_invalido(
    ruta_zip
):

    if not ruta_zip.exists():

        return


    fecha_hora = dt.datetime.now().strftime(

        "%Y%m%d_%H%M%S"

    )


    nueva_ruta = ruta_zip.with_name(

        ruta_zip.stem

        + "_INVALID_"

        + fecha_hora

        + ruta_zip.suffix

    )


    ruta_zip.replace(
        nueva_ruta
    )


    logger.warning(

        "RECOVERY | "
        "ZIP previo inválido movido a: %s",

        nueva_ruta

    )


# ============================================================
# PROCESO PRINCIPAL
# ============================================================

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report-date",
        help="Fecha especial YYYY-MM-DD"
    )
    args = parser.parse_args()

    fecha_reporte = obtener_fecha_reporte(
        args.report_date
    )


    nombre_zip = (

        f"{PREFIJO_REPORTE}_"
        f"{fecha_reporte}.zip"

    )


    carpeta_destino = (

        CARPETA_BASE
        / fecha_reporte

    )


    ruta_zip = (

        carpeta_destino
        / nombre_zip

    )


    ruta_part = Path(

        str(ruta_zip)
        + ".part"

    )


    carpeta_destino.mkdir(

        parents=True,

        exist_ok=True

    )


    logger.info(
        "=" * 70
    )


    logger.info(
        "INICIO LCE DOWNLOADER 4142 V2"
    )


    logger.info(

        "Fecha reporte: %s",

        fecha_reporte

    )


    logger.info(

        "Archivo esperado: %s",

        nombre_zip

    )


    logger.info(

        "Ventana máxima: %s minutos",

        MAX_WAIT_MINUTES

    )


    logger.info(

        "Intervalo consulta: %s segundos",

        POLL_INTERVAL_SECONDS

    )


    logger.info(
        "=" * 70
    )


    guardar_estado(

        "INICIANDO",

        "Inicializando proceso.",

        fecha_reporte=fecha_reporte,

        ruta_zip=ruta_zip

    )


    # ========================================================
    # CONFIGURACIÓN
    # ========================================================

    try:

        validar_configuracion()


    except Exception as error:


        logger.error(

            "CONFIG | %s",

            error

        )


        guardar_estado(

            "ERROR_CONFIG",

            str(error),

            EXIT_CONFIG_ERROR,

            fecha_reporte=fecha_reporte,

            ruta_zip=ruta_zip

        )


        return EXIT_CONFIG_ERROR


    # ========================================================
    # ¿YA EXISTE EL ZIP?
    # ========================================================

    if ruta_zip.exists():


        valido, mensaje, archivos = validar_zip(

            ruta_zip,

            fecha_reporte

        )


        if valido:


            logger.info(

                "CACHE | "
                "El ZIP ya existe y "
                "pasó todas las validaciones."

            )


            logger.info(

                "CACHE | %s",

                mensaje

            )


            guardar_estado(

                "OK_EXISTENTE",

                mensaje,

                EXIT_OK,

                fecha_reporte=fecha_reporte,

                ruta_zip=ruta_zip,

                tamano_bytes=
                ruta_zip.stat().st_size

            )


            return EXIT_OK


        logger.warning(

            "CACHE | "
            "Existe ZIP anterior "
            "pero es inválido: %s",

            mensaje

        )


        archivar_zip_invalido(
            ruta_zip
        )


    # ========================================================
    # LIMPIAR .PART ANTERIOR
    # ========================================================

    if ruta_part.exists():


        logger.warning(

            "RECOVERY | "
            "Se encontró un .part antiguo. "
            "Será eliminado."

        )


        ruta_part.unlink()


    # ========================================================
    # AUTENTICACIÓN
    # ========================================================

    try:


        token = obtener_token()


    except Exception as error:


        logger.error(

            "AUTH | %s",

            error

        )


        guardar_estado(

            "ERROR_AUTH",

            str(error),

            EXIT_AUTH_ERROR,

            fecha_reporte=fecha_reporte,

            ruta_zip=ruta_zip

        )


        return EXIT_AUTH_ERROR


    # ========================================================
    # VENTANA DE ESPERA
    # ========================================================

    inicio = time.monotonic()


    limite = (

        inicio

        + MAX_WAIT_MINUTES * 60

    )


    intento = 0


    while True:


        intento += 1


        logger.info(

            "METADATA | Intento %s",

            intento

        )


        guardar_estado(

            "BUSCANDO_REPORTE",

            (
                "Consultando disponibilidad "
                f"del reporte. Intento {intento}."
            ),

            intento=intento,

            fecha_reporte=fecha_reporte,

            ruta_zip=ruta_zip

        )


        # ====================================================
        # CONSULTAR REPORTE
        # ====================================================

        try:


            entry, token = buscar_reporte(

                token,

                nombre_zip

            )


        except Exception as error:


            logger.error(

                "METADATA | %s",

                error

            )


            if (

                time.monotonic()
                + POLL_INTERVAL_SECONDS

                <= limite

            ):


                logger.warning(

                    "METADATA | "
                    "Se volverá a intentar "
                    "en %s segundos.",

                    POLL_INTERVAL_SECONDS

                )


                guardar_estado(

                    "REINTENTO_METADATA",

                    str(error),

                    EXIT_METADATA_ERROR,

                    intento=intento,

                    fecha_reporte=
                    fecha_reporte,

                    ruta_zip=
                    ruta_zip

                )


                time.sleep(
                    POLL_INTERVAL_SECONDS
                )


                continue


            guardar_estado(

                "ERROR_METADATA",

                str(error),

                EXIT_METADATA_ERROR,

                intento=intento,

                fecha_reporte=
                fecha_reporte,

                ruta_zip=
                ruta_zip

            )


            return EXIT_METADATA_ERROR


        # ====================================================
        # REPORTE ENCONTRADO
        # ====================================================

        if entry:


            blob_uri = entry.get(
                "ReportBlobUri"
            )


            if not blob_uri:


                logger.error(

                    "METADATA | "
                    "El reporte apareció "
                    "pero no tiene ReportBlobUri."

                )


                guardar_estado(

                    "ERROR_METADATA",

                    "ReportBlobUri vacío.",

                    EXIT_METADATA_ERROR,

                    intento=intento,

                    fecha_reporte=
                    fecha_reporte,

                    ruta_zip=
                    ruta_zip

                )


                return EXIT_METADATA_ERROR


            logger.info(

                "METADATA | "
                "Reporte encontrado."

            )


            logger.info(

                "METADATA | "
                "URL segura de descarga obtenida."

            )


            # IMPORTANTE:
            # NO mostramos la SAS completa.


            guardar_estado(

                "REPORTE_ENCONTRADO",

                "Reporte disponible.",

                intento=intento,

                fecha_reporte=
                fecha_reporte,

                ruta_zip=
                ruta_zip

            )


            # =================================================
            # DESCARGAR
            # =================================================

            try:


                bytes_descargados = descargar_blob(

                    blob_uri,

                    ruta_part

                )


            except Exception as error:


                logger.error(

                    "DOWNLOAD | %s",

                    error

                )


                ruta_part.unlink(
                    missing_ok=True
                )


                # ---------------------------------------------
                # Puede haber expirado el SAS.
                # Volvemos a metadata para obtener uno nuevo.
                # ---------------------------------------------

                if (

                    time.monotonic()
                    + POLL_INTERVAL_SECONDS

                    <= limite

                ):


                    logger.warning(

                        "DOWNLOAD | "
                        "Se consultará metadata "
                        "nuevamente en %s segundos.",

                        POLL_INTERVAL_SECONDS

                    )


                    guardar_estado(

                        "REINTENTO_DOWNLOAD",

                        str(error),

                        EXIT_DOWNLOAD_ERROR,

                        intento=intento,

                        fecha_reporte=
                        fecha_reporte,

                        ruta_zip=
                        ruta_zip

                    )


                    time.sleep(
                        POLL_INTERVAL_SECONDS
                    )


                    continue


                guardar_estado(

                    "ERROR_DOWNLOAD",

                    str(error),

                    EXIT_DOWNLOAD_ERROR,

                    intento=intento,

                    fecha_reporte=
                    fecha_reporte,

                    ruta_zip=
                    ruta_zip

                )


                return EXIT_DOWNLOAD_ERROR


            # =================================================
            # VALIDAR
            # =================================================

            logger.info(

                "VALIDATION | "
                "Validando archivo descargado..."

            )


            valido, mensaje, archivos = validar_zip(

                ruta_part,

                fecha_reporte

            )


            if not valido:


                logger.error(

                    "VALIDATION | %s",

                    mensaje

                )


                # ---------------------------------------------
                # Conservar archivo inválido para diagnóstico
                # ---------------------------------------------

                fecha_hora = (
                    dt.datetime.now()
                    .strftime(
                        "%Y%m%d_%H%M%S"
                    )
                )


                archivo_invalido = (

                    carpeta_destino

                    / (

                        nombre_zip

                        + ".invalid_"

                        + fecha_hora

                    )

                )


                try:


                    ruta_part.replace(
                        archivo_invalido
                    )


                    logger.warning(

                        "VALIDATION | "
                        "Archivo inválido "
                        "conservado en: %s",

                        archivo_invalido

                    )


                except Exception:


                    ruta_part.unlink(
                        missing_ok=True
                    )


                if (

                    time.monotonic()
                    + POLL_INTERVAL_SECONDS

                    <= limite

                ):


                    logger.warning(

                        "VALIDATION | "
                        "Se volverá a descargar "
                        "en %s segundos.",

                        POLL_INTERVAL_SECONDS

                    )


                    guardar_estado(

                        "REINTENTO_VALIDACION",

                        mensaje,

                        EXIT_VALIDATION_ERROR,

                        intento=intento,

                        fecha_reporte=
                        fecha_reporte,

                        ruta_zip=
                        ruta_zip

                    )


                    time.sleep(
                        POLL_INTERVAL_SECONDS
                    )


                    continue


                guardar_estado(

                    "ERROR_VALIDATION",

                    mensaje,

                    EXIT_VALIDATION_ERROR,

                    intento=intento,

                    fecha_reporte=
                    fecha_reporte,

                    ruta_zip=
                    ruta_zip

                )


                return EXIT_VALIDATION_ERROR


            # =================================================
            # ZIP CORRECTO
            # =================================================

            logger.info(

                "VALIDATION | %s",

                mensaje

            )


            logger.info(

                "VALIDATION | "
                "Archivos internos: %s",

                len(archivos)

            )


            # =================================================
            # PUBLICACIÓN ATÓMICA
            # =================================================

            try:


                os.replace(

                    ruta_part,

                    ruta_zip

                )


            except Exception as error:


                logger.error(

                    "PUBLISH | "
                    "No fue posible publicar "
                    "ZIP definitivo: %s",

                    error

                )


                guardar_estado(

                    "ERROR_PUBLISH",

                    str(error),

                    EXIT_DOWNLOAD_ERROR,

                    intento=intento,

                    fecha_reporte=
                    fecha_reporte,

                    ruta_zip=
                    ruta_zip

                )


                return EXIT_DOWNLOAD_ERROR


            # =================================================
            # ÉXITO
            # =================================================

            tamano_final = (
                ruta_zip.stat().st_size
            )


            logger.info(

                "SUCCESS | "
                "ZIP publicado correctamente."

            )


            logger.info(

                "SUCCESS | Ruta: %s",

                ruta_zip

            )


            logger.info(

                "SUCCESS | Tamaño: %s bytes",

                tamano_final

            )


            logger.info(

                "SUCCESS | Intentos: %s",

                intento

            )


            guardar_estado(

                "OK",

                (
                    "Descarga y validación "
                    "completadas correctamente."
                ),

                EXIT_OK,

                intento=intento,

                fecha_reporte=
                fecha_reporte,

                ruta_zip=
                ruta_zip,

                tamano_bytes=
                tamano_final

            )


            return EXIT_OK


        # ====================================================
        # TODAVÍA NO EXISTE
        # ====================================================

        logger.warning(

            "WAIT | "
            "%s todavía no aparece "
            "en la API.",

            nombre_zip

        )


        ahora = time.monotonic()


        if (

            ahora
            + POLL_INTERVAL_SECONDS

            > limite

        ):


            mensaje = (

                f"El reporte {nombre_zip} "
                f"no apareció durante "
                f"{MAX_WAIT_MINUTES} minutos."

            )


            logger.error(

                "WAIT | %s",

                mensaje

            )


            guardar_estado(

                "REPORT_NOT_AVAILABLE",

                mensaje,

                EXIT_REPORT_NOT_AVAILABLE,

                intento=intento,

                fecha_reporte=
                fecha_reporte,

                ruta_zip=
                ruta_zip

            )


            return EXIT_REPORT_NOT_AVAILABLE


        logger.info(

            "WAIT | "
            "Próxima consulta en "
            "%s segundos.",

            POLL_INTERVAL_SECONDS

        )


        guardar_estado(

            "ESPERANDO_REPORTE",

            (
                "Reporte todavía no disponible. "
                f"Próxima consulta en "
                f"{POLL_INTERVAL_SECONDS} segundos."
            ),

            EXIT_REPORT_NOT_AVAILABLE,

            intento=intento,

            fecha_reporte=
            fecha_reporte,

            ruta_zip=
            ruta_zip

        )


        time.sleep(
            POLL_INTERVAL_SECONDS
        )


# ============================================================
# EJECUCIÓN
# ============================================================

if __name__ == "__main__":

    try:


        codigo_salida = main()


    except KeyboardInterrupt:


        logger.warning(

            "CANCEL | "
            "Proceso cancelado manualmente."

        )


        guardar_estado(

            "CANCELADO",

            "Proceso cancelado manualmente.",

            EXIT_UNEXPECTED_ERROR

        )


        codigo_salida = (
            EXIT_UNEXPECTED_ERROR
        )


    except Exception as error:


        logger.exception(

            "UNEXPECTED | "
            "Error inesperado: %s",

            error

        )


        guardar_estado(

            "ERROR_UNEXPECTED",

            str(error),

            EXIT_UNEXPECTED_ERROR

        )


        codigo_salida = (
            EXIT_UNEXPECTED_ERROR
        )


    logger.info(

        "END | "
        "Código de salida: %s",

        codigo_salida

    )


    sys.exit(
        codigo_salida
    )
