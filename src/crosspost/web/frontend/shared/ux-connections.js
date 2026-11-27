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
  function create(h,{useState,useEffect}) {
    return function ConnectionPanel({pid}) {
      const [attempt,setAttempt]=useState(0),[result,setResult]=useState(null);
      useEffect(()=>{
        if(!Number.isSafeInteger(pid)||pid<=0)return;
        const controller=new AbortController();
        setResult({pid,attempt,kind:"loading"});
        fetch("/api/profiles/"+pid+"/channels",{credentials:"same-origin",cache:"no-store",signal:controller.signal})
          .then(r=>{if(!r.ok)throw Error("Read failed");return r.json();})
          .then(data=>{if(!controller.signal.aborted)setResult({pid,attempt,kind:"ready",rows:rows(data)});})
          .catch(()=>{if(!controller.signal.aborted)setResult({pid,attempt,kind:"error"});});
        return ()=>controller.abort();
      },[pid,attempt]);
      const current=result?.pid===pid&&result.attempt===attempt?result:null;
      return h("section",{className:"cp-stack"},
        h("h1",{className:"cp-title"},"Подключения"),
        h("p",{className:"cp-muted"},"Здесь видно состояние площадок вашего профиля. Подключение выполняется в защищённом мастере с администратором. Не пересылайте пароли, коды и токены в сообщениях."),
        h("button",{className:"cp-button",onClick:()=>setAttempt(x=>x+1)},"Обновить состояния"),
        !pid?h("p",null,"Профиль пока не назначен."):!current||current.kind==="loading"?h("p",{role:"status"},"Загружаем подключения…"):
        current.kind==="error"?h("p",{role:"alert"},"Не удалось получить состояния подключений."):
        current.rows.length?current.rows.map(r=>{
          const info=describe(r.state);
          return h("article",{className:"cp-panel cp-stack",key:r.channel},
            h("div",{className:"cp-row"},h("strong",null,r.title),h("span",{className:"cp-status--"+info.tone},info.label)),
            h("p",{className:"cp-muted"},info.advice),
            r.expiresSoon&&r.state!=="banned"?h("p",{className:"cp-status--warning"},"Срок действия подключения скоро истечёт."):null,
            r.steps.length&&r.state!=="banned"?h("details",null,
              h("summary",null,"Этапы подключения"),
              h("p",{className:"cp-muted"},"Список для подготовки; действия выполняются в мастере."),
              h("ol",{className:"cp-connection-steps"},...r.steps.map((title,i)=>h("li",{key:i},title)))):null);
        }):h("p",null,"Нет доступных площадок."));
    };
  }
  window.CrosspostConnections=Object.freeze({describe,canManage,rows,create});
})();
