/* Same-screen task builder / numeric keypad. Existing right stick and A only. */
const vrCustom = {
  mode:'', catalog:{tasks:[],token:''}, draft:[], editIndex:0, libraryIndex:0,
  busy:false, inputField:'height', height:'0', head:'30', replaceDigit:true,
  title(){return this.mode==='pose'?'头部与身体位置':'自定义任务流程';},
  taskLabel(id){const t=this.catalog.tasks.find(t=>t.id===id);return t?`${t.location} · ${t.task}`:'任务已删除，请移除';},
  items(){
    const row=(kind,label,extra={})=>({kind,label,...extra});
    if(!this.mode)return null;
    if(this.mode==='pose')return [
      row('pose-field',`身体下降 ${this.height} cm`,{field:'height'}),
      row('pose-field',`头部俯视 ${this.head}°`,{field:'head'}),
      ...['1','2','3','4','5','6','7','8','9','.','0','⌫'].map(digit=>row('pose-digit',digit,{digit})),
      row('custom-close','返回菜单'),row('pose-stop','停止调整'),row('pose-apply','确认并调整')];
    if(this.mode==='confirm')return [row('custom-back','返回修改',{pane:'bar',row:0}),
      row('custom-start',`确认开始 ${this.draft.length} 项`,{pane:'bar',row:1})];
    return [
      ...(this.catalog.tasks.length?this.catalog.tasks.map((t,i)=>row('custom-add',this.taskLabel(t.id),{id:t.id,pane:'library',row:i}))
        :[row('custom-wait','任务库为空',{pane:'library',row:0})]),
      ...(this.draft.length?this.draft.map((id,i)=>row('custom-select',`${i+1}. ${this.taskLabel(id)}`,{pane:'draft',row:i}))
        :[row('custom-wait','从左侧添加任务',{pane:'draft',row:0})]),
      ...[['custom-close','返回菜单'],['custom-up','↑ 上移'],['custom-down','↓ 下移'],
        ['custom-remove','删除选中'],['custom-confirm',`开始 ${this.draft.length} 项`]]
        .map(([kind,label],i)=>row(kind,label,{pane:'bar',row:i}))];
  },
  focus(match){
    const index=this.items()?.findIndex(item=>Object.entries(match).every(([k,v])=>item[k]===v));
    if(index>=0)navUi.selected=index;
  },
  move(dx,dy){
    if(this.busy)return;
    const items=this.items()||[],current=items[navUi.selected];if(!current)return;
    if(this.mode==='pose'){
      const cells=this.poseCells(),c=cells[navUi.selected],cx=c.x+c.w/2,cy=c.y+c.h/2;
      const candidates=cells.map((p,i)=>{
        const x=p.x+p.w/2-cx,y=p.y+p.h/2-cy,along=dx?x*dx:y*dy,cross=dx?Math.abs(y):Math.abs(x);
        return {i,along,score:along+cross*2.5};
      }).filter(p=>p.along>10).sort((a,b)=>a.score-b.score);
      if(candidates.length)navUi.selected=candidates[0].i;
    }else if(this.mode==='confirm'){
      if(dx)navUi.selected=dx>0?1:0;
    }else if(current.pane==='bar'){
      if(dx)this.focus({pane:'bar',row:Math.max(0,Math.min(4,current.row+dx))});
      if(dy<0)this.focus({pane:'draft',row:Math.max(0,this.editIndex)});
    }else{
      if(dx>0&&current.pane==='library')this.focus({pane:'draft',row:Math.max(0,this.editIndex)});
      if(dx<0&&current.pane==='draft')this.focus({pane:'library',row:this.libraryIndex});
      if(dy){
        const count=items.filter(i=>i.pane===current.pane).length;
        if(dy>0&&current.row===count-1)this.focus({pane:'bar',row:current.pane==='draft'?3:0});
        else this.focus({pane:current.pane,row:Math.max(0,current.row+dy)});
      }
    }
    const chosen=items[navUi.selected];
    if(chosen?.pane==='library')this.libraryIndex=chosen.row;
    if(chosen?.pane==='draft')this.editIndex=chosen.row;
    drawNavigationPanel();
  },
  poseCells(){return [{x:240,y:257,w:298,h:84},{x:240,y:372,w:298,h:84},
    ...Array.from({length:12},(_,i)=>({x:570+(i%3)*122,y:257+Math.floor(i/3)*68,w:112,h:58})),
    ...Array.from({length:3},(_,i)=>({x:240+i*240,y:608,w:220,h:52}))];},
  numericValue(field){
    const raw=this[field],max=field==='height'?56:45;
    if(!/^\d{1,2}(?:\.\d)?$/.test(raw)||Number(raw)>max)
      throw Error(`${field==='height'?'下降高度':'俯视角度'}请输入0–${max}${field==='height'?'cm':'°'}，最多1位小数`);
    return Number(raw);
  },
  digit(key){
    let value=this[this.inputField];
    if(key==='⌫'){this[this.inputField]=value.slice(0,-1);this.replaceDigit=false;return;}
    if(this.replaceDigit)value='';
    if(key==='.'){
      if(value.includes('.'))return;
      value=(value||'0')+'.';
    }else{if(value==='0')value='';value+=key;}
    if(value.length>4||!/^(?:\d{0,2})(?:\.\d?)?$/.test(value))return;
    this[this.inputField]=value;this.replaceDigit=false;
  },
  async activate(item){
    if(!item.kind.startsWith('custom-')&&!item.kind.startsWith('pose-'))return false;
    if(this.busy)return true;
    const oldMode=this.mode;let focus=null;
    try{
      switch(item.kind){
        case 'custom-open':{
          this.busy=true;navShowNotice('正在读取任务库…',1200);
          const response=await fetch('/api/tasks/menu-catalog',{cache:'no-store'}),data=await response.json();
          if(!response.ok||!data.success)throw Error(data.message||'读取任务库失败');
          this.catalog=data;this.mode='sequence';this.libraryIndex=0;break;
        }
        case 'custom-close':this.mode='';break;
        case 'custom-back':this.mode='sequence';break;
        case 'custom-add':
          if(this.draft.length>=32)throw Error('每串最多32项');
          this.draft.push(item.id);this.editIndex=this.draft.length-1;focus={kind:'custom-add',id:item.id};break;
        case 'custom-select':this.editIndex=item.row;focus={kind:'custom-remove'};break;
        case 'custom-up':case 'custom-down':{
          const i=this.editIndex,j=i+(item.kind==='custom-up'?-1:1);
          if(i>=0&&i<this.draft.length&&j>=0&&j<this.draft.length){
            [this.draft[i],this.draft[j]]=[this.draft[j],this.draft[i]];this.editIndex=j;
          }
          focus={kind:item.kind};break;
        }
        case 'custom-remove':
          if(!this.draft.length)throw Error('当前还没有预编排任务');
          this.draft.splice(this.editIndex,1);this.editIndex=Math.max(0,Math.min(this.editIndex,this.draft.length-1));
          focus={kind:'custom-remove'};break;
        case 'custom-confirm':
          if(!this.draft.length)throw Error('请先添加任务');
          this.mode='confirm';break;
        case 'custom-start':
          this.busy=true;
          if(await navigationAction('/api/tasks/custom/start',{ids:this.draft,token:this.catalog.token,confirmed:true,client_id:state.clientInstanceId})){
            this.mode='';navUi.menuVisible=false;
          }break;
        case 'pose-open':{
          const p=navUi.state?.body_pose||{};
          this.height=String(Math.max(0,Math.min(56,p.lowering_cm??0)));
          this.head=String(Math.max(0,Math.min(45,p.head_pitch_deg??30)));
          this.inputField='height';this.replaceDigit=true;this.mode='pose';break;
        }
        case 'pose-field':this.inputField=item.field;this.replaceDigit=true;focus={kind:'pose-digit',digit:'1'};break;
        case 'pose-digit':this.digit(item.digit);break;
        case 'pose-apply':{
          const height=this.numericValue('height'),head=this.numericValue('head');
          this.busy=true;
          await navigationAction('/api/pose/preset',{body_lowering_cm:height,head_pitch_deg:head,client_id:state.clientInstanceId});break;
        }
        case 'pose-stop':await navigationAction('/api/pose/stop');break;
      }
    }catch(error){navShowNotice(error.message||String(error),3500,'#ff8b8b');}
    finally{this.busy=false;if(this.mode!==oldMode)navUi.selected=0;if(focus)this.focus(focus);drawNavigationPanel();}
    return true;
  },
  draw(context){
    context.save();
    const text=(label,x,y,width,size=20,color='#e7f7ff')=>{
      context.fillStyle=color;context.font=`${size}px sans-serif`;context.fillText(String(label),x,y,width);
    };
    const box=(x,y,w,h,selected=false,marked=false)=>{
      roundRect(context,x,y,w,h,10,selected?'#11627c':marked?'#27433f':'#132a38');
      if(marked){context.strokeStyle='#7ce9b8';context.lineWidth=2;context.strokeRect(x+1,y+1,w-2,h-2);}
    };
    const items=this.items()||[];
    if(this.mode==='pose'){
      roundRect(context,216,145,758,551,20,'rgba(4,18,27,0.97)');text('直接输入位置',240,188,600,27);
      const p=navUi.state?.body_pose||{};
      text(`当前实测：下降 ${p.lowering_cm??'—'} cm  /  俯视 ${p.head_pitch_deg??'—'}°`,240,225,690,20,'#80e7d0');
      this.poseCells().forEach((c,i)=>{
        const item=items[i],field=item.kind==='pose-field';
        box(c.x,c.y,c.w,c.h,i===navUi.selected,field&&item.field===this.inputField);
        if(field){
          text(item.field==='height'?'身体下降（0–56 cm）':'头部俯视（0–45°）',c.x+14,c.y+26,c.w-25,18,'#aac3cf');
          text(`${this[item.field]||'—'}${this.replaceDigit&&this.inputField===item.field?'  ▌':''}`,c.x+14,c.y+64,c.w-25,30);
        }else text(item.label,c.x+12,c.y+c.h/2+8,c.w-24,item.kind==='pose-digit'?26:20);
      });
      text(`数字键盘正在输入：${this.inputField==='height'?'身体下降厘米数':'头部俯视角度'}`,240,561,690,19,'#72d5ef');
      text(this.busy?'正在提交…':p.preset?.message||'输入不产生运动；点击“确认并调整”才执行',240,589,690,18);
      text('右摇杆选择 · A 输入/确认 · 俯视角度按硬件限位保护',240,684,690,16,'#a6bac5');
    }else{
      roundRect(context,126,127,948,591,20,'rgba(4,18,27,0.97)');
      text(this.mode==='confirm'?'确认任务顺序后开始':'自定义任务流程',150,170,880,28);
      text('任务库 · A 添加',150,210,420,20,'#7bdff4');text(`预编排顺序 · ${this.draft.length}/32 项`,610,210,420,20,'#7be9b8');
      const lists=[{pane:'library',x:150,entries:this.catalog.tasks.map(t=>({label:this.taskLabel(t.id)})),selected:this.libraryIndex},
        {pane:'draft',x:610,entries:this.draft.map((id,i)=>({label:`${i+1}. ${this.taskLabel(id)}`})),selected:this.editIndex}];
      for(const list of lists){
        const start=Math.max(0,Math.min(list.selected-3,Math.max(0,list.entries.length-8)));
        if(!list.entries.length)text(list.pane==='draft'?'← 从左侧添加，顺序实时显示':'任务库为空',list.x+14,277,410,20,'#91a6b5');
        list.entries.slice(start,start+8).forEach((entry,offset)=>{
          const row=start+offset,index=items.findIndex(i=>i.pane===list.pane&&i.row===row),y=229+offset*40;
          box(list.x,y,420,35,index===navUi.selected,list.pane==='draft'&&row===this.editIndex);
          text((list.pane==='library'?'+  ':'')+entry.label,list.x+10,y+25,398,19);
        });
        text(list.entries.length?`${start+1}–${Math.min(start+8,list.entries.length)} / ${list.entries.length}`:'0 项',list.x+14,569,400,16,'#91a6b5');
      }
      const current=this.draft[this.editIndex];
      text(current?`当前选中：第${this.editIndex+1}项  ${this.taskLabel(current)}`:'右侧选中任务后可直接删除或调整顺序',150,599,880,19,'#a1e6c9');
      const bars=items.filter(i=>i.pane==='bar'),w=880/bars.length;
      bars.forEach((item,i)=>{const index=items.indexOf(item),x=150+i*w;
        box(x,617,w-10,48,index===navUi.selected);text(item.label,x+12,647,w-34,20,item.kind==='custom-remove'?'#ffad9e':'#e7f7ff');});
      text(this.busy?'正在处理…':'右摇杆左右切换区域、上下选任务 · A 添加/操作 · 右摇杆键收起',150,698,880,17,'#a6bac5');
    }
    context.restore();
  }
};
