import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import {fileURLToPath} from 'node:url';
import {bundle} from '@remotion/bundler';
import {renderMedia, selectComposition} from '@remotion/renderer';

const base = path.dirname(fileURLToPath(import.meta.url));
const request = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const sources = new Map();
const mime = {'.mp4':'video/mp4','.mov':'video/quicktime','.m4v':'video/mp4','.webm':'video/webm','.mkv':'video/x-matroska','.wav':'audio/wav','.mp3':'audio/mpeg','.m4a':'audio/mp4','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'};
const server = http.createServer((req,res) => {
  if (!['GET','HEAD'].includes(req.method)) {res.writeHead(405);res.end();return;}
  const source = sources.get(new URL(req.url,'http://localhost').pathname);
  if (!source) {res.writeHead(404);res.end();return;}
  const size = fs.statSync(source).size;
  let start=0,end=size-1,status=200;
  const range=/^bytes=(\d+)-(\d*)$/.exec(req.headers.range || '');
  if (range) {start=Number(range[1]);end=range[2] ? Math.min(Number(range[2]),size-1):size-1;status=206;}
  if (start>end || start>=size || !Number.isSafeInteger(start) || !Number.isSafeInteger(end)) {res.writeHead(416,{'Content-Range':`bytes */${size}`});res.end();return;}
  const headers={'Content-Type':mime[path.extname(source).toLowerCase()]||'application/octet-stream','Content-Length':end-start+1,'Accept-Ranges':'bytes','Access-Control-Allow-Origin':'*'};
  if(status===206) headers['Content-Range']=`bytes ${start}-${end}/${size}`;
  res.writeHead(status,headers);
  if(req.method==='HEAD'){res.end();return;}
  const stream=fs.createReadStream(source,{start,end});
  stream.on('error',()=>res.destroy());
  res.on('close',()=>stream.destroy());
  stream.pipe(res);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const port=server.address().port;
const bundleDir=path.join(path.dirname(request.output),'remotion-bundle');
try {
  const props={...request};
  const routeFile=(name,file)=>{
    if(!file) return null;
    const target=path.resolve(file);
    if(!fs.existsSync(target)||!fs.statSync(target).isFile()) throw new Error(`素材文件不存在: ${name}`);
    const route=`/media/${name}`;sources.set(route,target);
    return `http://127.0.0.1:${port}${route}`;
  };
  for(const name of ['video','audio','bgm','image']) props[name]=routeFile(name,props[name]);
  props.pipItems=(props.pipItems || []).map((item,index)=>({...item,path:routeFile(`pip-${index}`,item.path)}));
  const serveUrl=await bundle({entryPoint:path.join(base,'src/index.jsx'),outDir:bundleDir});
  const options={serveUrl,inputProps:props,browserExecutable:process.env.MPT_RENDER_BROWSER || undefined};
  const composition=await selectComposition({...options,id:'CreatorVideo'});
  let reportedProgress=-1;
  await renderMedia({...options,composition,codec:'h264',outputLocation:request.output,concurrency:2,x264Preset:'veryfast',crf:20,overwrite:false,muted:Boolean(request.silent),offthreadVideoCacheSizeInBytes:256*1024*1024,mediaCacheSizeInBytes:256*1024*1024,onProgress:({progress})=>{const percent=Math.round(progress*100);if(percent!==reportedProgress){reportedProgress=percent;process.stdout.write(JSON.stringify({progress:percent})+'\n');}}});
  process.stdout.write(JSON.stringify({output:request.output})+'\n');
} finally {
  await new Promise(resolve=>{server.close(resolve);server.closeAllConnections();});
  fs.rmSync(bundleDir,{recursive:true,force:true});
}
