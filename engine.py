"""Execute existing LC scripts in isolated requests; never return logs/secrets."""
import csv, io, json, os, re, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path
from datetime import date
ROOT=Path(__file__).resolve().parent
class UserError(Exception): pass

def validate_spec(spec):
 if not isinstance(spec,dict): raise UserError('Solicitud inválida.')
 franchise=spec.get('franchise');kind=spec.get('kind');day=spec.get('date','')
 if franchise not in ('4142','04640'):raise UserError('Selecciona 4142 o 04640.')
 if kind not in ('sales','mix','both'):raise UserError('Selecciona Ventas, Mix o ambos.')
 try:
  if date.fromisoformat(day).isoformat()!=day:raise ValueError()
 except (ValueError,TypeError):raise UserError('La fecha debe tener formato YYYY-MM-DD.')
 stores=spec.get('stores',[])
 if not isinstance(stores,list) or len(stores)>100 or any(not isinstance(s,str) or not re.fullmatch(r'\d{1,12}',s) for s in stores):raise UserError('Las tiendas deben ser códigos o terminaciones numéricas.')
 if spec.get('all') is not True and not stores:raise UserError('Indica tiendas o selecciona todas.')
 return franchise,day,kind,stores

def inspect_zip(blob,franchise,day,kind):
 try:
  z=zipfile.ZipFile(io.BytesIO(blob))
  if len(z.infolist())>100 or sum(i.file_size for i in z.infolist())>150*1024*1024:raise UserError('ZIP demasiado grande al descomprimir.')
  for entry in z.infolist():
   p=Path(entry.filename.replace('\\','/'))
   if p.is_absolute() or '..' in p.parts or entry.flag_bits&1:raise UserError('ZIP no compatible.')
  if z.testzip() is not None:raise UserError('ZIP dañado.')
  files={}
  wanted=['detail-orders'] if kind=='sales' else []
  mix='summary-items' if franchise=='4142' else 'detail-orderlines'
  if kind=='mix':wanted=[mix]
  if kind=='both':wanted=['detail-orders',mix]
  for source in wanted:
   matches=[n for n in z.namelist() if Path(n).name.lower().startswith(source+'-') and n.lower().endswith('.csv')]
   if len(matches)!=1:raise UserError('No se encontró una fuente única de '+source+'.')
   stores=set()
   with z.open(matches[0]) as f:
    reader=csv.DictReader(io.TextIOWrapper(f,encoding='utf-8-sig'))
    if not reader.fieldnames:raise UserError('CSV sin encabezados.')
    reader.fieldnames=[c.lower().strip() for c in reader.fieldnames]
    if 'franchisestore' not in reader.fieldnames or 'businessdate' not in reader.fieldnames:raise UserError('La fuente requiere FranchiseStore y BusinessDate.')
    for row in reader:
     raw=row['franchisestore'].strip();parts=raw.split('-')
     if len(parts)!=2 or int(parts[0])!=int(franchise):raise UserError('El ZIP contiene otra franquicia.')
     if row['businessdate'].split('T')[0].split(' ')[0]!=day:raise UserError('El ZIP contiene otra fecha de negocio.')
     store=parts[0][3:5]+parts[1][1:]
     if not re.fullmatch(r'\d+',store):raise UserError('Código de tienda inválido.')
     stores.add(store)
   files[source]=stores
  return files
 except (zipfile.BadZipFile,UnicodeError,KeyError,ValueError):raise UserError('No se pudo validar el ZIP y sus fuentes.')

def resolve_stores(requested,all_stores,source_sets):
 available=set.union(*source_sets)
 if all_stores:selected=sorted(available)
 else:
  selected=[]
  for suffix in requested:
   matches=[s for s in sorted(available) if s==suffix] or [s for s in sorted(available) if s.endswith(suffix)]
   if not matches:raise UserError('No existe una tienda con código o terminación '+suffix+'.')
   if len(matches)>1:raise UserError('La terminación '+suffix+' coincide con '+', '.join(matches)+'. Indica el código completo.')
   if matches[0] not in selected:selected.append(matches[0])
 if not selected:raise UserError('No hay tiendas en el reporte.')
 if any(any(s not in group for group in source_sets) for s in selected):raise UserError('Alguna tienda no tiene todas las fuentes solicitadas.')
 return selected

def run_script(name,args,env):
 try:
  p=subprocess.run([sys.executable,str(ROOT/'scripts'/name),*args],env=env,capture_output=True,timeout=120)
 except subprocess.TimeoutExpired:raise UserError('La operación excedió el tiempo permitido. Intenta con un ZIP adjunto.')
 if p.returncode:raise UserError('No fue posible completar la descarga o generación. Verifica la configuración y disponibilidad del reporte.')

def generate(spec,blob=None):
 franchise,day,kind,requested=validate_spec(spec)
 with tempfile.TemporaryDirectory(prefix='lc-job-') as temp:
  root=Path(temp);reports=root/'reportes';out=root/'salida';directory=reports/day;directory.mkdir(parents=True);out.mkdir()
  env=os.environ.copy();env['LC_'+franchise+'_REPORTS_DIR']=str(reports);env['LC_'+franchise+'_OUTPUT_DIR']=str(out)
  env['LCE_04640_MAX_WAIT_MINUTES']='0';env['LC_4142_MAX_WAIT_MINUTES']='0'
  prefix='04142' if franchise=='4142' else '04640';zip_path=directory/(prefix+'_'+day+'.zip')
  if blob is None:
   required=['USERNAME','PASSWORD','HMAC_USER','HMAC_KEY']
   if any(not env.get('LC_'+franchise+'_'+key) for key in required):raise UserError('Las credenciales de la franquicia '+franchise+' aún no están configuradas.')
   run_script('ArchivosApi4142.py' if franchise=='4142' else 'Api04640.py',['--report-date',day],env)
   if not zip_path.is_file():raise UserError('El reporte no está disponible para esa fecha.')
   blob=zip_path.read_bytes()
  if len(blob)>25*1024*1024:raise UserError('El ZIP supera 25 MB.')
  sources=inspect_zip(blob,franchise,day,kind);selected=resolve_stores(requested,spec.get('all') is True,list(sources.values()));zip_path.write_bytes(blob)
  jobs=[]
  if kind in ('sales','both'):jobs.append(('ventas4142.py' if franchise=='4142' else 'ventas04640.py','vtas','002'))
  if kind in ('mix','both'):jobs.append(('archivomix4142.py' if franchise=='4142' else 'mix04640.py','mix','001'))
  for script,label,start in jobs:
   args=['--report-date',day,'--special']
   if len(selected)==1:args+=['--store',selected[0]]
   run_script(script,args,env)
  result=io.BytesIO()
  with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as output:
   for store in selected:
    for script,label,start in jobs:
     stamp=date.fromisoformat(day).strftime('%m%d%y' if franchise=='04640' and label=='vtas' else '%m%d%Y')
     filename=start+store+label+stamp+'.txt';p=out/filename
     if not p.is_file():raise UserError('No se generó el archivo solicitado para '+store+'.')
     content=p.read_bytes();lines=content.decode('utf-8').splitlines()
     expected_date=date.fromisoformat(day).strftime('%m/%d/%Y')
     if len(lines)<2 or not lines[0].startswith('H\t'+store+'\t') or expected_date not in lines[0]:raise UserError('Cabecera o registros inválidos para '+store+'.')
     for line in lines[1:]:
      cells=line.split('\t')
      if cells[0]!='D' or len(cells)!=(8 if label=='vtas' else 7):raise UserError('Formato de detalle inválido.')
      if any(re.fullmatch(r'[+-]?(nan|inf|infinity)',c.strip(),re.I) for c in cells):raise UserError('Se encontraron valores numéricos inválidos.')
     output.writestr(filename,content)
  return result.getvalue()
