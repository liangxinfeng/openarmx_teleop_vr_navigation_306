// Full production tick: menu ownership, long-A, and menu activation together.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const text=fs.readFileSync(process.argv[2],'utf8');
const start=text.indexOf('  tick: function (time, delta) {');
const end=text.indexOf('\n  }\n});',start);
assert(start>0&&end>start);
const tickBody=text.slice(start+'  tick: function (time, delta) {'.length,end);
const menuStart=text.indexOf("  return [\n    { kind: 'stop', label:");
const menuEnd=text.indexOf('\n}',menuStart);
const menu=new Function('navUi','const waypoints=[];'+text.slice(menuStart,menuEnd));
function scenario(open=true,kind='task-retry-pose'){
 const ui={visible:true,textureBound:true,menuVisible:open,previous:{},selected:0,
   state:{task_dispatch:{state:'failed',task_token:'chain:1'}},baseControlLatched:false,
   gripperWidthCm:0};
 ui.items=menu(ui);ui.selected=ui.items.findIndex(i=>i.kind===kind);
 assert(ui.selected>=0,'retry must be visible in the actual failed-task menu');
 const buttons=()=>Array.from({length:6},()=>({pressed:false}));
 const left={buttons:buttons(),axes:[0,0,0,0]},right={buttons:buttons(),axes:[0,0,0,0]};
 const calls=[],notices=[],activations=[];let now=0;
 const ctx={navUi:ui,performance:{now:()=>now},isPointCloudMode:()=>false,
   bindNavigationVideoTexture:()=>{},gamepadForHand:(_,hand)=>hand==='left'?left:right,
   handleDeliveryOfferInput:()=>false,handleDeliveryArrivalInput:()=>false,
   navShowNotice:t=>notices.push(t),navigationAction:(...args)=>{calls.push(args);return Promise.resolve();},
   drawNavigationPanel:()=>{},sendBaseDrive:()=>{},setBaseControlLatched:v=>{ui.baseControlLatched=v},
   activateNavigationItem:()=>activations.push(ui.items[ui.selected]?.kind),
   vrCustom:{mode:''},autolifeVrClientId:()=> 'headset'};
 vm.runInNewContext('tick=function(time,delta){'+tickBody+'};',ctx);
 const component={el:{renderer:{xr:{getSession:()=>({})}}}};
 return {ui,calls,notices,activations,tick(ms,a=false,x=false){
   now=ms;right.buttons[4].pressed=a;left.buttons[4].pressed=x;
   ctx.tick.call(component,ms,16);
 }};
}
for(const open of [false,true]){
 const s=scenario(open);s.tick(1,true);s.tick(1100,true);
 assert(!s.calls.length&&!s.activations.length,'holding must not select a menu action');
 s.tick(1200);s.tick(1300);
 assert.equal(s.calls.length,1);assert.equal(s.calls[0][0],'/api/tasks/retry-pose');
 assert.equal(s.calls[0][1].task_token,'chain:1');assert.equal(s.calls[0][1].client_id,'headset');
}
let s=scenario();s.tick(1,true);s.tick(400);assert(!s.calls.length&&!s.activations.length);
s=scenario(true,'stop');s.tick(1,true);s.tick(1100,true);s.tick(1200);
assert(!s.calls.length);assert.deepEqual(s.activations,['stop']);
for(const change of ['selection','task','x','drive','mode']){
 s=scenario();s.tick(1,true);s.tick(1100,true);
 if(change==='selection')s.ui.selected=0;
 if(change==='task')s.ui.state.task_dispatch.task_token='chain:2';
 if(change==='drive')s.ui.baseControlLatched=true;
 if(change==='mode')s.ui.state.task_dispatch.state='awaiting_action';
 s.tick(1200,false,change==='x');assert(!s.calls.length,change);
}
assert(text.includes('菜单选“重试当前姿态”'));
console.log('PASS: actual failed menu and full tick, open/closed menu retry, release once, short-A no motion, other menu isolation, changed selection/task/state, X chord and base ownership');
