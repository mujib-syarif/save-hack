import os, uuid, socket, ipaddress
from pathlib import Path
from urllib.parse import urlparse
from threading import BoundedSemaphore
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
import yt_dlp

app=FastAPI(title="Save Hack Media API",version="0.1.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=False,allow_methods=["GET","POST"],allow_headers=["Content-Type"])
OUT=Path(os.getenv("DOWNLOAD_DIR","/tmp/downloads")); OUT.mkdir(parents=True,exist_ok=True)
MAX_MB=int(os.getenv("MAX_DOWNLOAD_MB","300")); SEM=BoundedSemaphore(max(1,int(os.getenv("MAX_CONCURRENT_JOBS","1"))))
HOSTS={"youtube.com","www.youtube.com","youtu.be","m.youtube.com","tiktok.com","www.tiktok.com","vm.tiktok.com","vt.tiktok.com","instagram.com","www.instagram.com","facebook.com","www.facebook.com","fb.watch","x.com","www.x.com","twitter.com","www.twitter.com","reddit.com","www.reddit.com","v.redd.it","vimeo.com","www.vimeo.com","dailymotion.com","www.dailymotion.com"}
EXTS={".mp4",".m4v",".mov",".webm",".mp3",".m4a",".aac",".wav",".ogg"}
class MediaRequest(BaseModel):
 url:str=Field(min_length=8,max_length=2048)
 rights_confirmed:bool
class DownloadRequest(MediaRequest):
 format:str=Field(pattern="^(mp4|mp3)$")
def validate_url(raw):
 try:
  p=urlparse(raw.strip()); host=(p.hostname or "").lower().rstrip(".")
  if p.scheme!="https" or not host: raise ValueError()
  try:
   ip=ipaddress.ip_address(host)
   if not ip.is_global: raise ValueError()
   direct=True
  except ValueError:
   direct=Path(p.path.lower()).suffix in EXTS
   if host not in HOSTS and not direct: raise ValueError()
   for info in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM):
    if not ipaddress.ip_address(info[4][0]).is_global: raise ValueError()
  return raw.strip()
 except Exception: raise HTTPException(400,"URL tidak valid, alamat privat, atau domain belum didukung.")
def cleanup(path):
 try: path.unlink(missing_ok=True)
 except Exception: pass
def opts(job,fmt):
 o={"outtmpl":str(OUT/(job+".%(ext)s")),"noplaylist":True,"quiet":True,"no_warnings":True,"restrictfilenames":True,"socket_timeout":15,"retries":1,"fragment_retries":1,"max_filesize":MAX_MB*1024*1024}
 if fmt=="mp3":
  o.update({"format":"bestaudio/best","postprocessors":[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]})
 else:
  o.update({"format":"best[ext=mp4][vcodec^=avc1][acodec^=mp4a]/best[ext=mp4]/best","merge_output_format":"mp4"})
 return o
@app.get("/health")
def health(): return {"ok":True,"service":"Save Hack Media API","version":"0.1.0"}
@app.post("/analyze")
def analyze(req:MediaRequest):
 if not req.rights_confirmed: raise HTTPException(400,"Konfirmasi hak unduh wajib diisi.")
 url=validate_url(req.url)
 try:
  with yt_dlp.YoutubeDL({"quiet":True,"no_warnings":True,"noplaylist":True,"socket_timeout":12,"skip_download":True}) as y:
   i=y.extract_info(url,download=False)
  return {"title":i.get("title") or "Media tanpa judul","uploader":i.get("uploader") or i.get("channel") or "Tidak diketahui","duration":i.get("duration"),"thumbnail":i.get("thumbnail"),"extractor":i.get("extractor_key") or i.get("extractor"),"formats":["mp4","mp3"]}
 except Exception as e: raise HTTPException(422,"URL gagal dianalisis; sumber mungkin tidak didukung atau akses dibatasi. "+str(e)[:180])
@app.post("/download")
def download(req:DownloadRequest):
 if not req.rights_confirmed: raise HTTPException(400,"Konfirmasi hak unduh wajib diisi.")
 url=validate_url(req.url)
 if not SEM.acquire(blocking=False): raise HTTPException(429,"Server sibuk. Coba lagi sebentar.")
 job=uuid.uuid4().hex
 try:
  with yt_dlp.YoutubeDL(opts(job,req.format)) as y: y.download([url])
  found=[p for p in OUT.glob(job+".*") if p.is_file() and p.suffix not in {".part",".ytdl"}]
  if not found: raise HTTPException(422,"Engine tidak menghasilkan file.")
  path=max(found,key=lambda p:p.stat().st_mtime)
  if path.stat().st_size>MAX_MB*1024*1024:
   cleanup(path); raise HTTPException(413,f"File melebihi batas {MAX_MB} MB.")
  media="audio/mpeg" if path.suffix.lower()==".mp3" else "video/mp4"
  return FileResponse(path,media_type=media,filename="save-hack"+path.suffix,background=BackgroundTask(cleanup,path))
 except HTTPException: raise
 except Exception as e: raise HTTPException(422,"Download gagal: "+str(e)[:200])
 finally:
  SEM.release()
