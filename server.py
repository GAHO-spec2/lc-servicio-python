import hmac,json,os,threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from engine import generate,UserError
LIMIT=25*1024*1024
LOCK=threading.BoundedSemaphore(2)
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def respond(self,status,payload):
  body=json.dumps(payload,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def authorized(self):
  token=os.environ.get('LCE_BACKEND_TOKEN','');return len(token)>=32 and hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token)
 def do_GET(self):
  if not self.authorized():return self.respond(401,{'error':'Acceso no autorizado.'})
  if self.path!='/health':return self.respond(404,{'error':'Ruta no disponible.'})
  self.respond(200,{'status':'ok'})
 def do_POST(self):
  if not self.authorized():return self.respond(401,{'error':'Acceso no autorizado.'})
  if self.path!='/generate':return self.respond(404,{'error':'Ruta no disponible.'})
  try:size=int(self.headers.get('Content-Length','0'))
  except ValueError:return self.respond(400,{'error':'Solicitud inválida.'})
  if size<=0 or size>LIMIT:return self.respond(413,{'error':'Solicitud vacía o mayor a 25 MB.'})
  if not LOCK.acquire(blocking=False):return self.respond(429,{'error':'Hay solicitudes en proceso. Intenta nuevamente.'})
  try:
   raw=self.rfile.read(size);ct=self.headers.get('Content-Type','');blob=None
   if ct.startswith('multipart/form-data'):
    message=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+ct+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
    fields={}
    for part in message.iter_parts():fields[part.get_param('name',header='content-disposition')]=part.get_payload(decode=True)
    spec=json.loads(fields.get('request',b''));blob=fields.get('zip')
    if blob is None:raise UserError('Falta el ZIP adjunto.')
   elif ct.startswith('application/json'):spec=json.loads(raw)
   else:raise UserError('Tipo de solicitud no compatible.')
   result=generate(spec,blob);self.send_response(200);self.send_header('Content-Type','application/zip');self.send_header('Content-Disposition','attachment; filename="ventas-mix.zip"');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
  except UserError as e:self.respond(422,{'error':str(e)})
  except (ValueError,TypeError):self.respond(400,{'error':'Solicitud inválida.'})
  except Exception:self.respond(500,{'error':'No fue posible completar la generación.'})
  finally:LOCK.release()
if __name__=='__main__':
 if len(os.environ.get('LCE_BACKEND_TOKEN',''))<32:raise SystemExit('Configura LCE_BACKEND_TOKEN con al menos 32 caracteres.')
 ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
