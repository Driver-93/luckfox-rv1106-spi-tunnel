const pages=await fetch("http://127.0.0.1:9246/json").then(r=>r.json());
const page=pages.find(p=>p.type==="page");
const ws=new WebSocket(page.webSocketDebuggerUrl);
let id=0;const P=new Map();
function s(m,p={}){return new Promise((r,j)=>{const i=++id;P.set(i,{r,j});ws.send(JSON.stringify({id:i,method:m,params:p}));});}
ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&P.has(m.id)){const q=P.get(m.id);P.delete(m.id);m.error?q.j(m.error):q.r(m.result);}};
ws.onopen=async()=>{await s("Runtime.enable");await new Promise(r=>setTimeout(r,4000));
 const x=await s("Runtime.evaluate",{expression:"(()=>{const g=id=>document.getElementById(id);return {lat:g('lat')?g('lat').textContent:'-',cls:g('lat')?g('lat').className:'-',vmsg:g('vmsg')?g('vmsg').textContent:'-'};})()",returnByValue:true});
 console.log(JSON.stringify(x.result.value,null,2));ws.close();process.exit(0);};
ws.onerror=()=>process.exit(1);
setTimeout(()=>process.exit(1),16000);
