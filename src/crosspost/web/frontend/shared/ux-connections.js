/* Connection display is a snapshot, not an external availability guarantee. */
(() => {
  "use strict";
  const STATES=Object.freeze({
    live:{label:"Подключено",tone:"success",advice:"Последнее сохранённое состояние активно. Доступность площадки проверяется при публикации."},
    needs_relogin:{label:"Нужен повторный вход",tone:"warning",advice:"Откройте мастер повторного входа вместе с администратором."},
    not_connected:{label:"Не подключено",tone:"info",advice:"Подготовьте аккаунт и необходимые права для подключения."},
    banned:{label:"Ограничено площадкой",tone:"error",advice:"Не повторяйте отправку или вход автоматически. Обратитесь к администратору."},
    unknown:{label:"Состояние неизвестно",tone:"warning",advice:"Обновите данные. Если состояние не уточняется, обратитесь к администратору."}
  });
  const state=value=>Object.hasOwn(STATES,value)?value:"unknown";
  const describe=value=>STATES[state(value)];
  const canManage=value=>["live","needs_relogin","not_connected"].includes(value);
  function rows(data) {
    if(!Array.isArray(data)||data.some(r=>!r||typeof r.channel!=="string"||!r.channel||
      typeof r.title!=="string"||typeof r.state!=="string")||
      new Set(data.map(r=>r.channel)).size!==data.length) throw Error("Invalid channel list");
    return data.map(r=>({channel:r.channel,title:r.title,state:state(r.state),
      expiresSoon:r.expires_soon===true,
      steps:Array.isArray(r.guide?.steps)?r.guide.steps
        .filter(x=>x&&x.optional!==true&&typeof x.title==="string")
        .map(x=>x.title):[]}));
  }
  // Only public instructions from the existing declaration; no field values or
  // alternative session-import instructions are projected into the client view.
  function preparation(guide) {
    if(!Array.isArray(guide?.steps)) return [];
    return guide.steps.filter(x=>x&&x.optional!==true&&x.kind!=="tg_session"&&typeof x.title==="string")
      .map(x=>{
        const browser=x.kind==="browser_open"||x.kind==="browser_confirm";
        const body=browser?"Этот шаг выполняется в окне мастера с администратором. На телефоне окно серверного браузера может быть недоступно.":typeof x.body==="string"?x.body:"";
        let link="";
        try {
          const u=new URL(x.link);
          if(u.protocol==="https:"&&!u.username&&!u.password) link=u.href;
        } catch(_) {}
        return {title:x.title,body,landmark:!browser&&typeof x.landmark==="string"?x.landmark:"",
          link,linkLabel:typeof x.link_label==="string"&&x.link_label?x.link_label:"Открыть официальный раздел"};
      });
  }
  const needsAttention=r=>["needs_relogin","banned","unknown"].includes(r.state)||(r.state==="live"&&r.expiresSoon);
  function useConnections({useState,useEffect},pid) {
      const [attempt,setAttempt]=useState(0),[result,setResult]=useState(null);
      useEffect(()=>{
        if(!Number.isSafeInteger(pid)||pid<=0)return;
        const controller=new AbortController();
        setResult({pid,attempt,kind:"loading"});
        fetch("/api/profiles/"+pid+"/channels",{credentials:"same-origin",cache:"no-store",signal:controller.signal})
          .then(r=>{if(!r.ok)throw Error("Read failed");return r.json();})
          .then(data=>{if(!controller.signal.aborted)setResult({pid,attempt,kind:"ready",
            rows:rows(data).map((r,i)=>({...r,preparation:preparation(data[i].guide)}))});})
          .catch(()=>{if(!controller.signal.aborted)setResult({pid,attempt,kind:"error"});});
        return ()=>controller.abort();
      },[pid,attempt]);
      const current=result?.pid===pid&&result.attempt===attempt?result:null;
      return {...(!pid?{kind:"no-profile"}:current||{kind:"loading"}),retry:()=>setAttempt(x=>x+1)};
  }
  function create(h,hooks) {
    function TelegramConnection({pid,onDone}) {
      const [flow,setFlow]=hooks.useState(""),[step,setStep]=hooks.useState("begin");
      const [apiId,setApiId]=hooks.useState(""),[hash,setHash]=hooks.useState(""),[target,setTarget]=hooks.useState("");
      const [value,setValue]=hooks.useState(""),[busy,setBusy]=hooks.useState(false),[error,setError]=hooks.useState("");
      async function send(action,body) {
        const r=await window.CrosspostRequest.fetch("/api/profiles/"+pid+"/client-telegram/"+action,{
          method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
        if(!r.ok)throw Error(r.status===429?"Слишком много попыток. Попробуйте позже.":"Вход не подтверждён. Обновите состояние и начните заново позже.");
        return r.json();
      }
      async function submit(event) {
        event.preventDefault();if(busy)return;setBusy(true);setError("");
        try {
          const result=step==="begin"?await send("begin",{api_id:Number(apiId),api_hash:hash.trim(),target:target.trim()}):
            await send(step,{flow,[step]:value});
          if(result.step==="done"&&result.state==="live"){onDone();return;}
          if(!["phone","code","password"].includes(result.step))throw Error("Неизвестный шаг");
          if(step==="begin"){if(!/^[A-Za-z0-9_-]{43}$/.test(result.flow))throw Error("Не удалось начать вход");setFlow(result.flow);}
          setStep(result.step);
        }catch(e){setError(e.message||"Нет связи. Проверьте подключения перед повтором.");setStep("failed");}
        finally{setHash("");setValue("");setBusy(false);}
      }
      async function cancel() {
        if(busy)return;setBusy(true);
        try{if(flow)await send("cancel",{flow});}catch(_){}
        finally{setFlow("");setValue("");setHash("");setStep("begin");setError("");setBusy(false);}
      }
      return h("form",{className:"cp-stack",onSubmit:submit,autoComplete:"off"},
        h("p",{className:"cp-muted"},"Подключайте только свой Telegram. Вход действует 5 минут. Не пересылайте коды и пароль в переписке."),
        step==="begin"?h("div",{className:"cp-stack"},
          h("a",{href:"https://my.telegram.org/apps",target:"_blank",rel:"noopener noreferrer"},"Получить api_id и api_hash"),
          h("label",null,"api_id",h("input",{className:"cp-input",required:true,inputMode:"numeric",value:apiId,onInput:e=>setApiId(e.target.value)})),
          h("label",null,"api_hash",h("input",{className:"cp-input",type:"password",required:true,maxLength:32,value:hash,onInput:e=>setHash(e.target.value)})),
          h("label",null,"Канал назначения",h("input",{className:"cp-input",required:true,placeholder:"@mychannel",value:target,onInput:e=>setTarget(e.target.value)}))):null,
        ["phone","code","password"].includes(step)?h("label",null,step==="phone"?"Номер телефона (+…)":step==="code"?"Код из Telegram":"Облачный пароль Telegram",
          h("input",{key:step,className:"cp-input",type:step==="phone"?"tel":"password",required:true,autoComplete:step==="code"?"one-time-code":"off",value,onInput:e=>setValue(e.target.value)})):null,
        error?h("p",{role:"alert"},error):null,
        step!=="failed"?h("button",{className:"cp-button cp-button--primary",type:"submit",disabled:busy},busy?"Выполняем…":step==="phone"?"Получить код":"Продолжить"):null,
        flow||step==="failed"?h("button",{className:"cp-button",type:"button",disabled:busy,onClick:cancel},"Отменить / начать заново"):null);
    }
    function InitialConnection({pid,channel,onDone}) {
      const [token,setToken]=hooks.useState(""),[ig,setIg]=hooks.useState("");
      const [busy,setBusy]=hooks.useState(false),[message,setMessage]=hooks.useState("");
      async function submit(event) {
        event.preventDefault();
        if(busy||!token.trim())return;
        setBusy(true);setMessage("");
        try {
          const r=await window.CrosspostRequest.fetch("/api/profiles/"+pid+"/client-connection/"+channel,{
            method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({token:token.trim(),ig_user_id:ig.trim()})});
          if(!r.ok) {
            setMessage(r.status===409?"Состояние изменилось. Обновите данные.":r.status===429?"Слишком много попыток. Попробуйте позже.":"Подключение не подтверждено. Проверьте данные или обратитесь к администратору.");
            return;
          }
          if((await r.json()).state!=="live")throw Error("Unconfirmed");
          onDone();
        } catch(_) {setMessage("Результат неизвестен. Обновите состояния перед повтором.");}
        finally {setToken("");setIg("");setBusy(false);}
      }
      return h("form",{className:"cp-stack",onSubmit:submit,autoComplete:"off"},
        h("p",{className:"cp-muted"},"Вставьте токен из официального кабинета. Он проверяется до сохранения и не отображается повторно."),
        channel==="instagram"?h("label",null,"Instagram user ID",h("input",{className:"cp-input",required:true,maxLength:32,inputMode:"numeric",value:ig,onInput:e=>setIg(e.target.value)})):null,
        h("label",null,"Токен доступа",h("input",{className:"cp-input",type:"password",required:true,maxLength:8192,autoComplete:"off",value:token,onInput:e=>setToken(e.target.value)})),
        h("button",{className:"cp-button cp-button--primary",type:"submit",disabled:busy||!token.trim()},busy?"Проверяем…":"Проверить и подключить"),
        message?h("p",{role:"alert"},message):null);
    }
    return function ConnectionPanel({pid}) {
      const current=useConnections(hooks,pid);
      const [filter,setFilter]=hooks.useState("all");
      const [caps,setCaps]=hooks.useState(null),[open,setOpen]=hooks.useState(null);
      hooks.useEffect(()=>{
        if(!pid)return;
        const controller=new AbortController();
        setCaps(null);setOpen(null);
        fetch("/api/profiles/"+pid+"/client-connection/capabilities",{credentials:"same-origin",cache:"no-store",signal:controller.signal})
          .then(r=>r.ok?r.json():null).then(value=>{
            if(!controller.signal.aborted)setCaps({pid,channels:Array.isArray(value?.channels)?value.channels:[]});
          }).catch(()=>{});
        return ()=>controller.abort();
      },[pid]);
      const allowed=caps?.pid===pid?caps.channels:[];
      const list=current.rows||[];
      const visible=filter==="attention"?list.filter(needsAttention):filter==="setup"?list.filter(r=>r.state==="not_connected"):list;
      return h("section",{className:"cp-stack"},
        h("h1",{className:"cp-title"},"Подключения"),
        h("p",{className:"cp-muted"},"Здесь видно состояние площадок вашего профиля. Если доступно самостоятельное подключение, воспользуйтесь формой. Для остальных площадок обратитесь к администратору. Не пересылайте пароли, коды и токены в сообщениях."),
        h("div",{className:"cp-panel cp-stack"},
          h("button",{className:"cp-button",onClick:current.retry},"Обновить состояния"),
          h("label",null,"Показать площадки",
            h("select",{className:"cp-input",value:filter,onChange:e=>setFilter(e.target.value)},
              h("option",{value:"all"},"Все"),h("option",{value:"attention"},"Требуют внимания"),
              h("option",{value:"setup"},"Не подключены"))),
          current.kind==="ready"?h("p",{className:"cp-muted"},"Подключено: "+list.filter(r=>r.state==="live").length+
            " из "+list.length+" · Требуют внимания: "+list.filter(needsAttention).length):null),
        !pid?h("p",null,"Профиль пока не назначен."):!current||current.kind==="loading"?h("p",{role:"status"},"Загружаем подключения…"):
        current.kind==="error"?h("p",{role:"alert"},"Не удалось получить состояния подключений."):
        visible.length?visible.map(r=>{
          const info=describe(r.state);
          return h("article",{className:"cp-panel cp-stack",key:r.channel},
            h("div",{className:"cp-row"},h("strong",null,r.title),h("span",{className:"cp-status--"+info.tone},info.label)),
            h("p",{className:"cp-muted"},info.advice),
            r.expiresSoon&&r.state!=="banned"?h("p",{className:"cp-status--warning"},"Срок действия подключения скоро истечёт."):null,
            r.state==="not_connected"&&allowed.includes(r.channel)?h("div",{className:"cp-stack"},
              h("button",{className:"cp-button",onClick:()=>setOpen(open===r.channel?null:r.channel)},open===r.channel?"Свернуть":"Подключить самостоятельно"),
              open===r.channel?h(r.channel==="telegram"?TelegramConnection:InitialConnection,{key:pid+":"+r.channel,pid,channel:r.channel,onDone:()=>{setOpen(null);current.retry();}}):null):null,
            r.preparation.length&&canManage(r.state)?h("details",null,
              h("summary",null,"Этапы подключения"),
              h("p",{className:"cp-muted"},"Это инструкция, а не форма входа. Вводите секреты только в защищённом мастере. Не передавайте их администратору в переписке."),
              h("ol",{className:"cp-connection-steps"},...r.preparation.map((step,i)=>h("li",{key:i,className:"cp-stack"},
                h("strong",null,step.title),
                step.body?h("p",null,step.body):null,
                step.landmark?h("p",{className:"cp-muted"},step.landmark):null,
                step.link?h("a",{href:step.link,target:"_blank",rel:"noopener noreferrer"},step.linkLabel):null)))):null);
        }):h("p",null,filter==="all"?"Нет доступных площадок.":"Нет площадок в выбранной категории."));
    };
  }
  window.CrosspostConnections=Object.freeze({describe,canManage,rows,preparation,needsAttention,useConnections,create});
})();
