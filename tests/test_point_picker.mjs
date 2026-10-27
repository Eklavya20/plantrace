import test from "node:test";
import assert from "node:assert/strict";
import {clientToPixel,mountPicker,nextSelection,selectionValue,streamlitMessage} from "../plantrace/point_picker_frontend/picker.mjs";

test("CSS zoom and scrolled client bounds map to intrinsic preview pixels",()=>{
  assert.deepEqual(clientToPixel(260,170,{left:10,top:20,width:500,height:300},1000,600),{x:500,y:300});
  assert.deepEqual(clientToPixel(400,100,{left:-100,top:-200,width:1000,height:600},1000,600),{x:500,y:300});
  assert.deepEqual(clientToPixel(510,320,{left:10,top:20,width:1000,height:600},1000,600),{x:500,y:300});
});

test("third click starts a new reference and payload retains frame/region/reset",()=>{
  const a={x:1,y:2},b={x:3,y:4},c={x:5,y:6};
  assert.deepEqual(nextSelection(nextSelection([],a),b),[a,b]);
  assert.deepEqual(nextSelection([a,b],c),[c]);
  assert.deepEqual(selectionValue("frame",7,[a,b],"region"),{frame:"frame",reset:7,points:[a,b],region_id:"region"});
  assert.equal(streamlitMessage("componentReady",{apiVersion:1}).isStreamlitMessage,true);
});

function harness(){
  const handlers={},messages=[];
  const ctx=new Proxy({},{get:(target,key)=>target[key]||(()=>{}),set:(target,key,value)=>(target[key]=value,true)});
  const elements={
    plan:{style:{},width:300,height:150,getContext:()=>ctx,
      addEventListener:(name,fn)=>handlers[`canvas:${name}`]=fn,
      getBoundingClientRect(){return {left:10,top:20,width:parseFloat(this.style.width),height:parseFloat(this.style.height)};}},
    zoom:{value:"100",addEventListener:(name,fn)=>handlers[`zoom:${name}`]=fn},
    zoomValue:{},viewport:{clientWidth:608},status:{},
  };
  const parent={postMessage:(message)=>messages.push(message)};
  const win={parent,Image:class{set src(value){this.onload();}},addEventListener:(name,fn)=>handlers[name]=fn};
  mountPicker(win,{getElementById:id=>elements[id]});
  const args={image:"local-png",width:1200,height:1000,frame:"source-frame",reset:0,
    region:{id:"region-a",name:"Page",bounds:[0,0,1200,1000]},transform:[1,0,0,1,0,0],guides:[]};
  const render=()=>handlers.message({source:parent,data:{type:"streamlit:render",args}});
  return {handlers,messages,elements,args,render,parent};
}

test("component handshake and two clicks use actual rendering/click handlers",()=>{
  const h=harness();
  assert.equal(h.messages[0].type,"streamlit:componentReady");
  assert.equal(h.messages[0].apiVersion,1);
  h.render();
  h.handlers["canvas:click"]({clientX:60,clientY:70});
  h.render(); // Streamlit rerun must retain the first point.
  h.handlers["canvas:click"]({clientX:460,clientY:370});
  const message=h.messages.at(-1);
  assert.equal(message.type,"streamlit:setComponentValue");
  assert.equal(message.dataType,"json");
  assert.deepEqual(message.value,{frame:"source-frame",reset:0,region_id:"region-a",points:[{x:100,y:100},{x:900,y:700}]});
});

test("region/reset changes clear points and zoom never changes stored source-frame pixels",()=>{
  const h=harness();h.render();
  h.handlers["canvas:click"]({clientX:60,clientY:70});
  h.args.region={...h.args.region,id:"region-b"};h.render();
  h.elements.zoom.value="200";h.handlers["zoom:input"]();
  h.handlers["canvas:click"]({clientX:110,clientY:120});
  assert.deepEqual(h.messages.at(-1).value.points,[{x:100,y:100}]);
  assert.equal(h.messages.at(-1).value.region_id,"region-b");
  h.args.reset=1;h.render();h.handlers["canvas:click"]({clientX:210,clientY:220});
  assert.deepEqual(h.messages.at(-1).value.points,[{x:200,y:200}]);
});

test("keyboard selection emits the same validated point protocol",()=>{
  const h=harness();h.render();
  h.handlers["canvas:keydown"]({key:"ArrowRight",shiftKey:true,preventDefault(){}});
  h.handlers["canvas:keydown"]({key:"ArrowDown",shiftKey:false,preventDefault(){}});
  h.handlers["canvas:keydown"]({key:"Enter",preventDefault(){}});
  assert.deepEqual(h.messages.at(-1).value.points,[{x:10,y:1}]);
});

test("messages from a different window cannot change the preview source frame",()=>{
  const h=harness();h.render();
  h.handlers.message({source:{},data:{type:"streamlit:render",args:{...h.args,frame:"wrong"}}});
  h.handlers["canvas:click"]({clientX:60,clientY:70});
  assert.equal(h.messages.at(-1).value.frame,"source-frame");
});
