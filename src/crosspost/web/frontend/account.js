(() => {
  "use strict";
  const content = document.getElementById("content");
  const status = document.getElementById("status");
  const heading = document.getElementById("heading");
  const description = document.getElementById("description");
  const fragment = location.hash.slice(1);
  // Remove capabilities from the visible address/history before doing any IO.
  if (fragment !== "owner") history.replaceState(null, "", "/account");
  const params = new URLSearchParams(fragment);
  let token = params.get("invite") || params.get("recover") || "";
  const mode = params.has("invite") ? "invite" : params.has("recover") ? "recover" : "";
  function element(tag, text, attrs = {}) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }
  function field(form, label, attrs) {
    const row = element("label", label);
    const input = element("input", "", attrs);
    row.append(input); form.append(row); return input;
  }
  function button(parent, label, action) {
    const b = element("button", label, {type:"button"});
    b.addEventListener("click", action); parent.append(b); return b;
  }
  async function request(path, body) {
    const opts = {credentials:"same-origin", cache:"no-store"};
    if (body !== undefined) Object.assign(opts, {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    const r = await fetch(path, opts);
    if (!r.ok) {
      const messages = {400:"Ссылка недействительна или устарела.",401:"Требуется вход.",
        403:"Доступ запрещён или неверный пароль.",409:"Логин или название недоступны.",
        422:"Проверьте поля.",503:"Регистрация пока недоступна."};
      throw Error(messages[r.status] || "Запрос не выполнен. Проверьте состояние перед повтором.");
    }
    return r.json();
  }
  function showError(err) { status.textContent = err.message || "Нет связи с сервисом."; }
  function client(kind, capability) {
    content.replaceChildren(); token = capability;
    heading.textContent = kind === "invite" ? "Создайте аккаунт" : "Установите новый пароль";
    description.textContent = "Пароль знаете только вы. Не передавайте его владельцу или администратору.";
    const form = element("form", "");
    const login = kind === "invite" ? field(form,"Логин",{required:"",minlength:"3",
      maxlength:"64",autocomplete:"username",autocapitalize:"none"}) : null;
    const pass = field(form,"Новый пароль (от 12 символов)",{type:"password",required:"",
      minlength:"12",maxlength:"256",autocomplete:"new-password"});
    const confirm = field(form,"Повторите пароль",{type:"password",required:"",
      minlength:"12",maxlength:"256",autocomplete:"new-password"});
    const submit = element("button","Сохранить",{type:"submit"}); form.append(submit);
    form.addEventListener("submit", async e => {
      e.preventDefault(); if (submit.disabled) return;
      if (pass.value !== confirm.value) { status.textContent = "Пароли не совпадают."; return; }
      submit.disabled = true; status.textContent = "";
      try {
        const body = {token,new_password:pass.value};
        if (login) body.login = login.value.trim();
        await request(kind === "invite" ? "/api/auth/register" : "/api/auth/recover", body);
        token = ""; content.replaceChildren();
        heading.textContent = "Готово";
        description.textContent = "Теперь войдите с новым паролем.";
        status.textContent = "Пароль сохранён.";
      } catch(err) { showError(err); }
      finally { pass.value = ""; confirm.value = ""; submit.disabled = false; }
    });
    content.append(form);
  }
  function ownerLink(raw, kind) {
    const link = location.origin + "/account#" + kind + "=" + encodeURIComponent(raw);
    const panel = element("article","");
    panel.append(element("p","Передайте ссылку только клиенту по согласованному приватному каналу."));
    const output = field(panel,"Одноразовая ссылка",{readonly:""});
    output.value = link;
    button(panel,"Копировать",async () => {
      try { await navigator.clipboard.writeText(link); status.textContent = "Ссылка скопирована."; }
      catch (_) { output.select(); status.textContent = "Выделите и скопируйте ссылку вручную."; }
    });
    button(panel,"Скрыть ссылку",() => panel.remove());
    content.append(panel);
  }
  async function supportCard() {
    const panel = element("section", "");
    panel.append(element("h2", "Карточка клиента"));
    panel.append(element("p", "Режим поддержки: просмотр от имени владельца. Настройки здесь не изменяются."));
    const selection = element("select", "", {"aria-label":"Профиль для поддержки"});
    selection.append(element("option", "Выберите профиль", {value:""}));
    const result = element("article", "");
    panel.append(selection);
    let epoch = 0;
    const open = button(panel, "Открыть карточку", async () => {
      const pid = Number(selection.value);
      if (!Number.isSafeInteger(pid) || pid <= 0 || open.disabled) return;
      const current = ++epoch;
      open.disabled = true;
      result.replaceChildren(element("p", "Загружаем карточку…"));
      try {
        const card = await request("/api/users/support/" + pid + "/inspect", {});
        if (current !== epoch || Number(selection.value) !== pid) return;
        if (card.profile_id !== pid) throw Error("Получен другой профиль.");
        const labels = {live:"Подключено",needs_relogin:"Нужен повторный вход",banned:"Ограничено",
          done:"Опубликовано",submitted:"Отправлено без подтверждения",failed:"Не опубликовано",
          new:"Ожидает",queued:"В очереди",attempting:"Выполняется",scheduled:"Запланировано",
          pending_approval:"На проверке",publishing:"Публикуется",cancelled:"Отменено"};
        const label = value => Object.hasOwn(labels,value) ? labels[value] : "Неизвестно";
        result.replaceChildren(element("h3", "Поддержка · " + card.profile_name),
          element("p", "Профиль №" + pid),
          element("p", "Аккаунты: " + (card.accounts.map(x=>x.login).join(", ") || "Не созданы")),
          element("p", "Модерация: " + (card.moderation_enabled ? "Включена" : "Выключена")),
          element("h3", "Сохранённые подключения"));
        if (!card.connections.length) result.append(element("p", "Нет сохранённых состояний."));
        for (const row of card.connections) result.append(element("p", row.channel + " — " + label(row.state)));
        result.append(element("h3", "Расписание"));
        if (!Object.keys(card.scheduled_counts).length) result.append(element("p", "Нет записей."));
        for (const [state,count] of Object.entries(card.scheduled_counts)) {
          result.append(element("p", label(state) + ": " + count));
        }
        result.append(element("h3", "Последние 10 сохранённых результатов по площадкам"));
        if (!card.recent_outcomes.length) result.append(element("p", "Нет сохранённых результатов."));
        for (const row of card.recent_outcomes) result.append(element("p", row.channel + " — " + label(row.status)));
        result.append(element("p", "Это наблюдаемые записи, а не проверка доставки или доступности площадок."));
        result.append(element("a", "Вернуться к панели управления", {href:"/"}));
      } catch (err) {
        if (current === epoch) result.replaceChildren(element("p", err.message || "Не удалось открыть карточку."));
      } finally {
        if (current === epoch) open.disabled = !selection.value;
      }
    });
    open.disabled = true;
    selection.addEventListener("change", () => {
      ++epoch; result.replaceChildren(); open.disabled = !selection.value;
    });
    button(panel,"Закрыть карточку",()=>{
      ++epoch; selection.value=""; result.replaceChildren(); open.disabled=true;
    });
    panel.append(result); content.append(panel);
    try {
      const profiles = await request("/api/profiles");
      for (const p of profiles) selection.append(element("option", p.name, {value:String(p.id)}));
    } catch (err) { result.replaceChildren(element("p", err.message)); }
  }
  async function owner() {
    heading.textContent = "Приглашения и восстановление";
    description.textContent = "Только для владельца. Вы не получаете пароль клиента.";
    try {
      const me = await request("/api/auth/me");
      if (me.role !== "admin") throw Error("Доступно только владельцу.");
      await supportCard();
      const clients = await request("/api/users");
      const recovery = element("form","");
      recovery.append(element("h2","Восстановить доступ"));
      const choice = element("select","",{"aria-label":"Клиент",required:""});
      for (const u of clients.filter(x=>x.role==="client"&&x.profile_id!==null)) {
        choice.append(element("option",u.login,{value:String(u.id)}));
      }
      recovery.append(choice);
      const password = field(recovery,"Ваш пароль",{type:"password",required:"",autocomplete:"current-password"});
      const send = element("button","Создать ссылку восстановления",{type:"submit"});
      recovery.append(send); send.disabled = !choice.options.length;
      recovery.addEventListener("submit",async e=>{
        e.preventDefault(); if(send.disabled)return; send.disabled=true;
        try { const r=await request("/api/users/"+Number(choice.value)+"/recovery-token",{admin_password:password.value});
          ownerLink(r.token,"recover"); }
        catch(err){showError(err);}finally{password.value="";send.disabled=false;}
      });
      content.append(recovery);
      let slots;
      try { slots=await request("/api/users/invitations"); }
      catch(err) { content.append(element("p",err.message)); return; }
      const issue = element("form","");
      issue.append(element("h2","Пригласить нового клиента"));
      const name=field(issue,"Название нового профиля",{required:"",maxlength:"255"});
      const admin=field(issue,"Ваш пароль",{type:"password",required:"",autocomplete:"current-password"});
      const create=element("button","Создать приглашение",{type:"submit"}); issue.append(create);
      issue.addEventListener("submit",async e=>{
        e.preventDefault(); if(create.disabled)return; create.disabled=true;
        try{const r=await request("/api/users/invitations",{profile_name:name.value.trim(),admin_password:admin.value});
          ownerLink(r.token,"invite");name.value="";await refresh();}
        catch(err){showError(err);}finally{admin.value="";create.disabled=false;}
      });
      content.append(issue);
      const list=element("section","");content.append(list);
      function render(items){
        list.replaceChildren(element("h2","Выданные приглашения"));
        const labels={active:"Действует",used:"Использовано",expired:"Истекло",revoked:"Отозвано"};
        for(const item of items){
          const row=element("article","");row.append(element("strong",item.profile_name),
            element("span",labels[item.state]||"Неизвестно"));
          if(item.state!=="used"){
            const p=field(row,"Ваш пароль",{type:"password",autocomplete:"current-password"});
            let busy=false;
            for(const [action,label] of [["renew","Перевыпустить"],["revoke","Отозвать"]]){
              button(row,label,async()=>{
                if(busy||!p.value)return;busy=true;
                try{const r=await request("/api/users/invitations/"+Number(item.id)+"/"+action,{admin_password:p.value});
                  if(action==="renew")ownerLink(r.token,"invite");
                  await refresh();
                }catch(err){showError(err);}finally{p.value="";busy=false;}
              });
            }
          }
          list.append(row);
        }
      }
      async function refresh(){render(await request("/api/users/invitations"));}
      render(slots);
    } catch(err) { showError(err); }
  }
  if(mode && token && !(params.has("invite")&&params.has("recover")))client(mode,token);
  else if(fragment==="owner")owner();
  else{
    description.textContent="Откройте одноразовую ссылку, которую вы получили от владельца. Если её нет, обратитесь к нему.";
  }
  window.addEventListener("pagehide",()=>{token="";content.replaceChildren();});
  window.addEventListener("pageshow",e=>{if(e.persisted)location.reload();});
})();
