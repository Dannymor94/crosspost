/* Read-only calendar projection; date arithmetic is civil, not an instant conversion. */
(() => {
  "use strict";
  function parseDay(day) {
    if(typeof day!=="string"||!/^\d{4}-\d{2}-\d{2}$/.test(day)) throw Error("Invalid day");
    const date=new Date(day+"T00:00:00Z");
    if(!Number.isFinite(date.getTime())||date.toISOString().slice(0,10)!==day) throw Error("Invalid day");
    return date;
  }
  const dayString=date=>date.toISOString().slice(0,10);
  function addDays(day,count) {
    if(!Number.isSafeInteger(count)) throw Error("Invalid day offset");
    const date=parseDay(day); date.setUTCDate(date.getUTCDate()+count); return dayString(date);
  }
  function period(day,mode) {
    const date=parseDay(day);
    if(mode==="list") return {start:null,end:null};
    if(mode==="day") return {start:day,end:addDays(day,1)};
    if(mode==="week") {
      const start=addDays(day,-((date.getUTCDay()+6)%7));
      return {start,end:addDays(start,7)};
    }
    if(mode==="month") {
      const start=day.slice(0,7)+"-01";
      date.setUTCDate(1); date.setUTCMonth(date.getUTCMonth()+1);
      return {start,end:dayString(date)};
    }
    throw Error("Invalid period");
  }
  function shift(day,mode,direction) {
    if(![-1,1].includes(direction)) throw Error("Invalid direction");
    if(mode==="day"||mode==="week") return addDays(day,direction*(mode==="week"?7:1));
    if(mode==="month") {
      const date=parseDay(day.slice(0,7)+"-01");
      date.setUTCMonth(date.getUTCMonth()+direction);return dayString(date);
    }
    throw Error("Invalid period");
  }
  function canonicalDay(row,zone) {
    if(row.basis!=="utc"||!zone||typeof row.when!=="string"||
       !/(Z|[+-]\d\d:\d\d)$/.test(row.when)||!Number.isFinite(Date.parse(row.when))) return null;
    try {
      const wall=window.CrosspostTime.wallInput(row.when,zone);
      const day=wall.slice(0,10);parseDay(day);return day;
    } catch(_) { return null; }
  }
  function project(rows,{day,mode="list",zone,status="all"}) {
    if(!Array.isArray(rows)||!["all","scheduled","pending_approval"].includes(status)) throw Error("Invalid calendar");
    const bounds=period(day,mode), groups=new Map(),unplaced=[];
    for(const row of rows) {
      if(status!=="all"&&row.status!==status) continue;
      const key=canonicalDay(row,zone);
      if(!key){unplaced.push(row);continue;}
      if(bounds.start&&(key<bounds.start||key>=bounds.end))continue;
      if(!groups.has(key))groups.set(key,[]);
      groups.get(key).push(row);
    }
    return {bounds,days:[...groups.keys()].sort().map(day=>({day,rows:groups.get(day)})),unplaced};
  }
  window.CrosspostCalendar=Object.freeze({addDays,period,shift,canonicalDay,project});
})();
