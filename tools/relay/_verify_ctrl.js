const list = await fetch('http://127.0.0.1:9228/json').then(r=>r.json());
const page = list.find(p=>p.url.includes('8095'));
if(!page){ console.log('no cuff page: '+JSON.stringify(list.map(p=>p.url))); process.exit(1);}
const ws=new WebSocket(page.webSocketDebuggerUrl);
let id=0;const pending=new Map();
function send(m,p={}){return new Promise((r,j)=>{const m2=++id;pending.set(m2,{resolve:r,reject:j});ws.send(JSON.stringify({id:m2,method:m,params:p}));});}
ws.onmessage=(ev)=>{const m=JSON.parse(ev.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);}};
ws.onopen=async()=>{
  await send('Runtime.enable');
  const expr=`(()=>{
    const f=document.getElementById('video-frame');
    const st=document.getElementById('status').textContent;
    const conn=document.getElementById('conn').textContent;
    const hasIframe=f!==null;
    let iframeLoaded=false;
    try{ iframeLoaded= (f && (f.srcObject || f.contentDocument)); }catch(e){ iframeLoaded='cross-origin-aika'; }
    return { url: location.href, hasIframe, iframeSrc: f? f.src : null, iframeLoaded, statusText:st, conn:conn,
             btnCount: document.querySelectorAll('.btn').length };
  })()`;
  const res=await send('Runtime.evaluate',{expression:expr,returnByValue:true});
  console.log(JSON.stringify(res.result.value,null,2));
  ws.close();process.exit(0);
};
ws.onerror=()=>process.exit(1);
setTimeout(()=>process.exit(1),15000);
