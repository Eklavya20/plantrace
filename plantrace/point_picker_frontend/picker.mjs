// No remote scripts or network calls. Streamlit v1's documented bidirectional protocol.
export function clientToPixel(x, y, bounds, width, height) {
  if (!(bounds.width > 0 && bounds.height > 0)) throw new Error("Preview has no displayed size");
  return {x: Math.max(0, Math.min(width, (x - bounds.left) * width / bounds.width)),
          y: Math.max(0, Math.min(height, (y - bounds.top) * height / bounds.height))};
}

export function nextSelection(previous, point) {
  return previous.length >= 2 ? [point] : [...previous, point];
}

export function streamlitMessage(type, fields = {}) {
  return {isStreamlitMessage: true, type: `streamlit:${type}`, ...fields};
}

export function selectionValue(frame, reset, points, regionId) {
  return {frame, reset, points, region_id:regionId};
}

export function mountPicker(win, doc) {
  const canvas = doc.getElementById("plan"), ctx = canvas.getContext("2d");
  const zoom = doc.getElementById("zoom"), zoomValue = doc.getElementById("zoomValue");
  const viewport = doc.getElementById("viewport"), status = doc.getElementById("status");
  let args = null, points = [], image = null, resetKey = null, loadedImage = null, loaded = false;
  let cursor = {x: 0, y: 0};
  const send = (type, fields) => win.parent.postMessage(streamlitMessage(type, fields), "*");
  const map = p => {
    const [a,b,c,d,e,f] = args.transform;
    return {x:a*p.x+c*p.y+e, y:b*p.x+d*p.y+f};
  };
  function drawLine(ends, color, label) {
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 3;
    ctx.beginPath(); ends.forEach((p,i) => i ? ctx.lineTo(p.x,p.y) : ctx.moveTo(p.x,p.y)); ctx.stroke();
    for (const [index,p] of ends.entries()) {
      ctx.beginPath(); ctx.arc(p.x,p.y,6,0,Math.PI*2); ctx.fill();
      ctx.fillText(label || String(index+1),p.x+9,p.y-9);
    }
  }
  function redraw() {
    if (!args || !image || !loaded) return;
    canvas.width = args.width; canvas.height = args.height;
    const scale = Math.max(0.05, (viewport.clientWidth-8) / args.width) * Number(zoom.value)/100;
    canvas.style.width = `${args.width*scale}px`; canvas.style.height = `${args.height*scale}px`;
    ctx.drawImage(image,0,0,args.width,args.height); ctx.font = "18px sans-serif";
    const [x0,y0,x1,y1] = args.region.bounds;
    const corners = [map({x:x0,y:y0}),map({x:x1,y:y0}),map({x:x1,y:y1}),map({x:x0,y:y1})];
    ctx.strokeStyle = "#147d73"; ctx.lineWidth = 2; ctx.setLineDash([10,6]);
    ctx.beginPath(); corners.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y)); ctx.closePath(); ctx.stroke(); ctx.setLineDash([]);
    for (const guide of args.guides) drawLine(guide.points.map(map),guide.conflicting?"#bb2735":"#18735c",guide.label);
    if (points.length) drawLine(points,"#b12a84",null);
    status.textContent = `${args.region.name}: ${points.length}/2 points selected. Click or use arrow keys + Enter. Coordinates are stored in the source drawing frame.`;
    send("setFrameHeight",{height:470});
  }
  function choose(point) {
    if (!args || !image || !loaded) return;
    points = nextSelection(points,point); cursor = point; redraw();
    send("setComponentValue",{value:selectionValue(args.frame,args.reset,points,args.region.id),dataType:"json"});
  }
  canvas.addEventListener("click", event => {
    choose(clientToPixel(event.clientX,event.clientY,canvas.getBoundingClientRect(),canvas.width,canvas.height));
  });
  canvas.addEventListener("keydown", event => {
    const step = event.shiftKey?10:1;
    if (event.key === "Enter" || event.key === " ") {event.preventDefault(); choose({...cursor}); return;}
    const delta = {ArrowLeft:[-step,0],ArrowRight:[step,0],ArrowUp:[0,-step],ArrowDown:[0,step]}[event.key];
    if (!delta) return;
    event.preventDefault(); cursor = {x:Math.max(0,Math.min(canvas.width,cursor.x+delta[0])),y:Math.max(0,Math.min(canvas.height,cursor.y+delta[1]))};
    redraw(); ctx.strokeStyle="#b12a84"; ctx.strokeRect(cursor.x-6,cursor.y-6,12,12);
  });
  zoom.addEventListener("input",()=>{zoomValue.textContent=`${zoom.value}%`;redraw();});
  win.addEventListener("resize",redraw);
  win.addEventListener("message", event => {
    if (event.source !== win.parent || event.data?.type !== "streamlit:render") return;
    args = event.data.args;
    const token = `${args.frame}:${args.region.id}:${args.reset}`;
    if (token !== resetKey) {points=[];cursor={x:0,y:0};resetKey=token;}
    if (loadedImage !== args.image) {
      loadedImage=args.image; loaded=false; image=new win.Image();
      const pendingImage=image; image.onload=()=>{if(image===pendingImage){loaded=true;redraw();}};
      image.src=`data:image/png;base64,${args.image}`;
    } else redraw();
  });
  send("componentReady",{apiVersion:1});
  send("setFrameHeight",{height:470});
}

if (typeof window !== "undefined" && typeof document !== "undefined") mountPicker(window, document);
