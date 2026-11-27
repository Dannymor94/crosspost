/* Shared read-only calendar UI; hosts retain existing editing and publish actions. */
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
  function create(h,{useState,useEffect}) {
    return function CalendarPanel({pid,onCreate,onManage}) {
      const result=window.CrosspostOverview.useOverview({useState,useEffect},pid);
      const data=result.data;
      const [mode,setMode]=useState("list"),[anchor,setAnchor]=useState(""),[status,setStatus]=useState("all");
      const zone=validZone(data?.zone)?data.zone:null;
      const today=zone?window.CrosspostTime.wallInput(new Date().toISOString(),zone).slice(0,10):"";
      const day=anchor||today||"2000-01-01";
      const effective=zone?mode:"list";
      const cal=window.CrosspostCalendar;
      const projection=data?cal.project(data.rows,{day,mode:effective,zone,status}):null;
      function row(r) {
        return h("article",{className:"cp-overview-row",key:r.id},
          h("small",null,window.CrosspostOverview.timeLabel(r,zone)),
          h("strong",null,r.text.slice(0,180)||"Пост без текста"),
          h("span",{className:"cp-muted"},r.status==="scheduled"?"Запланировано":"На проверке"),
          r.channels?.length?h("small",null,r.channels.join(" · ")):null);
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
        h("div",{className:"cp-panel cp-stack"},body));
    };
  }
  window.CrosspostCalendarView=Object.freeze({create});
})();
