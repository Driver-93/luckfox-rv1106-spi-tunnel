const ws=new WebSocket("ws://127.0.0.1:9248/devtools/page/036C5974291893CF414AED1C8F0F686C");
let id=0;const P=new Map();
function s(m,p={}){return new Promise((r,j)=>{const i=++id;P.set(i,{r,j});ws.send(JSON.stringify({id:i,method:m,params:p}));});}
const errors=[];
ws.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.id&&P.has(m.id)){const q=P.get(m.id);P.delete(m.id);m.error?q.j(m.error):q.r(m.result);}
  else if(m.method==='Runtime.exceptionThrown'){ errors.push('EXC: '+(m.params.exceptionDetails&&m.params.exceptionDetails.text||'')); }
  else if(m.method==='Log.entryAdded'){ errors.push('LOG: '+m.params.entry.level+': '+(m.params.entry.text||'')); }
  else if(m.method==='Runtime.consoleAPICalled'){ const a=(m.params.args||[]).map(x=>x.value||x.description||'').join(' '); errors.push('CONSOLE: '+a); }
};
ws.onopen=async()=>{
  await s('Runtime.enable'); await s('Log.enable'); await s('Page.enable');
  // 閲嶆柊鍔犺浇浠ユ崟鑾峰姞杞芥湡鎶ラ敊
  await s('Page.reload',{ignoreCache:true});
  await new Promise(r=>setTimeout(r,9000));
  const x=await s('Runtime.evaluate',{expression:`(()=>{
    const g=id=>document.getElementById(id);
    const v=g('video');
    const pad=g('pad')||document.querySelector('.pad');
    return {
      vw: window.innerWidth,
      vmsg: g('vmsg')?g('vmsg').textContent:'-',
      lat: g('lat')?g('lat').textContent:'-',
      conn: g('conn')?g('conn').textContent:'-',
      status: g('status')?g('status').textContent:'-',
      videoReady: v?v.readyState:-1, videoW: v?v.videoWidth||0:0, videoH:v?v.videoHeight||0:0,
      padW: pad?pad.offsetWidth:0, padH: pad?pad.offsetHeight:0,
      bodyScrollW: document.body.scrollWidth, bodyClientW: document.body.clientWidth,
      btnCount: document.querySelectorAll('.btn').length,
      trackCount: v&&v.srcObject?v.srcObject.getTracks().length:0
    };})()`,returnByValue:true});
  console.log('STATE='+JSON.stringify(x.result.value,null,2));
  console.log('ERRORS='+JSON.stringify(errors,null,2));
  ws.close();process.exit(0);
};
ws.onerror=()=>process.exit(1);
setTimeout(()=>process.exit(1),20000);

