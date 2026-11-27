/* Calendar projection and explicit, CAS-protected time-only move. */
(() => {
  "use strict";
  const MODES=[["list","Список"],["day","День"],["week","Неделя"],["month","Месяц"]];
  const STATUSES=[["all","Все статусы"],["scheduled","Запланировано"],["pending_approval","На проверке"]];
  function validZone(zone) {
    if(!zone) return false;
    try { new Intl.DateTimeFormat("ru",{timeZone:zone}).format(); return true; } catch (_) { return false; }
  }
  function dateLabel(day) {
    return new Intl.DateTimeFormat("ru-RU",{timeZone:"UTC",day:"numeric",month:"long",year:"numeric"}).format(new Date(day+"T00:00:00Z"));
  }
  function create(h,{useState,useEffect,useRef}) {
    return function CalendarPanel({pid,onCreate,onManage,onCreateTime,canMove=false}) {
      const result=window.CrosspostOverview.useOverview({useState,useEffect},pid);
      const data=result.data;
      const [mode,setMode]=useState("list"),[anchor,setAnchor]=useState(""),[status,setStatus]=useState("all");
      const zone=validZone(data?.zone)?data.zone:null;
      const today=zone?window.CrosspostTime.wallInput(new Date().toISOString(),zone).slice(0,10):"";
      const day=anchor||today||"2000-01-01";
      const effective=zone?mode:"list";
      const [createTime,setCreateTime]=useState("");
      const [moving,setMoving]=useState(null),[newWall,setNewWall]=useState("");
      const [busy,setBusy]=useState(false),[notice,setNotice]=useState(null);
      const flight=useRef(false),mounted=useRef(true);
      useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
      async function move() {
        if(flight.current||!moving||!newWall||!canMove||zone!==moving.zone)return;
        flight.current=true;setBusy(true);setNotice(null);
        try {
          const response=await fetch("/api/profiles/"+pid+"/scheduled/"+moving.id+"/move",{
            method:"POST",credentials:"same-origin",cache:"no-store",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({expected_at:moving.when,wall:newWall,zone})
          });
          const body=await response.json().catch(()=>null);
          if(!mounted.current)return;
          if(response.status===422) {
            setNotice({error:true,text:typeof body?.detail==="string"?body.detail:"Некорректное время."});
            return;
          }
          if(response.status===409||response.status===403) {
            setNotice({error:true,text:"Перенос не выполнен: статус, время или правила изменились. Календарь обновлён."});
          } else if(response.ok&&body?.id===moving.id&&body.status==="scheduled"&&
            typeof body.scheduled_at==="string"&&/(Z|[+-]\d\d:\d\d)$/.test(body.scheduled_at)&&
            window.CrosspostTime.wallInput(body.scheduled_at,zone)===newWall) {
            setNotice({error:false,text:"Новое время сохранено."});
          } else throw Error("Unknown result");
          setMoving(null);result.retry();
        } catch(_) {
          if(mounted.current) {
            setMoving(null);setNotice({error:true,text:"Результат переноса неизвестен. Проверьте обновлённое расписание; повтор автоматически не выполняется."});result.retry();
          }
        } finally {
          flight.current=false;if(mounted.current)setBusy(false);
        }
      }
      const cal=window.CrosspostCalendar;
      const projection=data?cal.project(data.rows,{day,mode:effective,zone,status}):null;
      function row(r) {
        return h("article",{className:"cp-overview-row",key:r.id},
          h("small",null,window.CrosspostOverview.timeLabel(r,zone)),
          h("strong",null,r.text.slice(0,180)||"Пост без текста"),
          h("span",{className:"cp-muted"},r.status==="scheduled"?"Запланировано":"На проверке"),
          r.channels?.length?h("small",null,r.channels.join(" · ")):null,
          canMove&&zone&&r.status==="scheduled"&&cal.canonicalDay(r,zone)?
            h("button",{type:"button",className:"cp-button",disabled:busy,
              onClick:()=>{setNotice(null);setMoving({...r,zone});setNewWall(window.CrosspostTime.wallInput(r.when,zone));}},"Перенести"):null);
      }
      const buttons=h("div",{className:"cp-calendar-controls"},
        h("button",{type:"button",className:"cp-button cp-button--primary",onClick:onCreate},"Создать пост"),
        h("button",{type:"button",className:"cp-button",onClick:onManage},"Управление расписанием"));
      let body;
      if(result.kind==="loading") body=h("p",{role:"status"},"Загружаем расписание…");
      else if(result.kind==="error") body=h("div",{className:"cp-stack"},
        h("p",{role:"alert"},"Не удалось загрузить расписание."),
        h("button",{className:"cp-button",onClick:result.retry},"Повторить загрузку"));
      else if(!data) body=h("p",null,"Выберите профиль.");
      else {
        const cells=[];
        if(zone&&["week","month"].includes(effective)) {
          if(effective==="month") {
            const offset=(new Date(projection.bounds.start+"T00:00:00Z").getUTCDay()+6)%7;
            for(let i=0;i<offset;i++) cells.push(h("span",{key:"pad-"+i,"aria-hidden":true}));
          }
          for(let date=projection.bounds.start;date<projection.bounds.end;date=cal.addDays(date,1)) {
            const count=projection.days.find(g=>g.day===date)?.rows.length||0;
            cells.push(h("button",{type:"button",className:"cp-calendar-cell",key:date,
              "aria-label":dateLabel(date)+", записей: "+count,
              onClick:()=>{setAnchor(date);setMode("day");}},
              h("strong",null,Number(date.slice(8))),h("small",null,count+" пост.")));
          }
        }
        body=h("div",{className:"cp-stack"},
          h("p",{className:"cp-muted"},zone?"Часовой пояс: "+zone:"Часовой пояс не настроен или недоступен. Записи показаны без распределения по датам."),
          h("div",{className:"cp-calendar-controls",role:"group","aria-label":"Период календаря"},
            ...MODES.map(([id,label])=>h("button",{type:"button",key:id,className:"cp-button",
              disabled:!zone&&id!=="list","aria-pressed":effective===id,onClick:()=>setMode(id)},label))),
          h("label",{className:"cp-stack"},"Статус",
            h("select",{className:"cp-input","aria-label":"Статус",value:status,onChange:e=>setStatus(e.target.value)},
              ...STATUSES.map(([id,label])=>h("option",{key:id,value:id},label)))),
          zone&&effective!=="list"?h("div",{className:"cp-calendar-controls"},
            h("button",{type:"button",className:"cp-button","aria-label":"Предыдущий период",onClick:()=>setAnchor(cal.shift(day,effective,-1))},"‹"),
            h("input",{type:"date",className:"cp-input","aria-label":"Дата календаря",min:"1900-01-01",max:"9998-12-31",value:day,
              onChange:e=>{try {const next=e.target.value;cal.period(next,effective);setAnchor(next);}catch(_) {}}}),
            h("button",{type:"button",className:"cp-button","aria-label":"Следующий период",onClick:()=>setAnchor(cal.shift(day,effective,1))},"›"),
            h("button",{type:"button",className:"cp-button",onClick:()=>setAnchor(today)},"Сегодня")):null,
          zone&&effective==="day"&&onCreateTime?h("div",{className:"cp-calendar-controls"},
            h("input",{type:"time",className:"cp-input","aria-label":"Время нового поста",value:createTime,onChange:e=>setCreateTime(e.target.value)}),
            h("button",{type:"button",className:"cp-button cp-button--primary",disabled:busy||!/^\d\d:\d\d$/.test(createTime),
              onClick:()=>onCreateTime({wall:day+"T"+createTime,zone})},"Создать пост на выбранное время")):null,
          cells.length?h("div",{className:"cp-calendar-grid"},
            ...["Пн","Вт","Ср","Чт","Пт","Сб","Вс"].map(label=>h("small",{key:label,className:"cp-calendar-weekday"},label)),...cells):null,
          projection.days.length?projection.days.map(group=>h("section",{className:"cp-panel cp-stack",key:group.day},
            h("h3",null,dateLabel(group.day)),...group.rows.map(row))):
            h("p",{className:"cp-muted"},"В выбранном периоде нет записей."),
          projection.unplaced.length?h("section",{className:"cp-panel cp-stack"},
            h("h3",null,"Записи без точной даты профиля"),
            h("p",{className:"cp-muted"},"Старое время или недоступный часовой пояс. Дата не пересчитана."),
            ...projection.unplaced.map(row)):null);
      }
      return h("section",{className:"cp-stack"},h("h1",{className:"cp-title"},"Календарь"),buttons,
        notice?h("p",{role:notice.error?"alert":"status",className:notice.error?"cp-status--error":"cp-status--success"},notice.text):null,
        moving?h("section",{className:"cp-panel cp-stack"},
          h("h2",null,"Перенос публикации"),
          h("p",null,moving.text.slice(0,180)||"Пост без текста"),
          h("p",{className:"cp-muted"},"Часовой пояс: "+moving.zone),
          h("input",{type:"datetime-local",className:"cp-input","aria-label":"Новое время публикации",value:newWall,disabled:busy,onChange:e=>setNewWall(e.target.value)}),
          h("div",{className:"cp-calendar-controls"},
            h("button",{className:"cp-button cp-button--primary",disabled:busy||!canMove||zone!==moving.zone||!newWall||newWall===window.CrosspostTime.wallInput(moving.when,moving.zone),onClick:move},busy?"Сохраняем…":"Подтвердить перенос"),
            h("button",{className:"cp-button",disabled:busy,onClick:()=>setMoving(null)},"Отмена"))):null,
        h("div",{className:"cp-panel cp-stack"},body));
    };
  }
  window.CrosspostCalendarView=Object.freeze({create});
})();
