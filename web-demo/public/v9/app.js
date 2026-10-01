/* CoreTeams v9: local, deterministic UX prototype. No network requests or AI calls. */
(() => {
  "use strict";
  const $ = (s, root = document) => root.querySelector(s);
  const esc = (v = "") =>
    String(v).replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const media = (name) =>
    $(`#media-${name}`, $("#media-assets").content).getAttribute("src");
  const KEY = "coreteams-v9";
  const MAX_PROJECTS = 3;
  const courseStages = [
    {
      short: "Диагностика",
      title: "Карта проблем и концепция",
      next: "Перейти к программе",
      cost: 60,
      time: "3–5 минут",
      result: "Что стоит изменить в курсе и какую логику обучения выбрать.",
    },
    {
      short: "Программа",
      title: "Обновлённая программа",
      next: "Перейти к первому модулю",
      cost: 70,
      time: "5–8 минут",
      result:
        "Последовательность модулей, практика и ожидаемый результат каждого.",
    },
    {
      short: "Первый модуль",
      title: "Первый модуль курса",
      next: "Принять итоговый результат",
      cost: 50,
      time: "4–6 минут",
      result: "Сценарий занятия, практическое задание и критерии проверки.",
    },
  ];
  const courseQuestions = [
    {
      title: "Что сейчас важнее всего улучшить?",
      help: "Можно выбрать несколько пунктов. Это поможет команде расставить приоритеты.",
      options: [
        "Больше практики",
        "Понятнее объяснять сложное",
        "Помочь ученикам дойти до конца",
        "Обновить содержание",
      ],
      guide: "Здесь не нужен идеальный ответ",
      copy: "Вы знаете свой курс лучше всех. Отметьте то, что замечали сами. Если причин пока не видно — мы начнём с диагностики.",
    },
    {
      title: "Кто будет учиться на курсе?",
      help: "Какой опыт уже есть у ваших учеников? Можно выбрать несколько групп.",
      options: [
        "Начинают с нуля",
        "Уже знают основы",
        "Работают в этой области",
        "Аудитория смешанная",
      ],
      guide: "Посмотрим глазами ученика",
      copy: "Команда проверит, какие знания можно считать исходными, а что нужно объяснить. От этого зависит сложность первого задания.",
    },
    {
      title: "Что обязательно нужно сохранить?",
      help: "Обозначьте границы: команда не будет менять всё подряд.",
      options: [
        "Авторские примеры и кейсы",
        "Длительность курса",
        "Основные темы",
        "Стиль и тон автора",
      ],
      guide: "Решение остаётся за вами",
      copy: "Мы предложим изменения и объясним их смысл. Переход к следующему результату — только после вашего одобрения.",
    },
  ];
  const courseSuggestions = [
    "Начать с проверки учебной логики",
    "Смешанная аудитория — требуется проверка",
    "Сохранить авторские кейсы — требуется подтверждение",
  ];
  const icons = {
    arrow: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
    back: '<path d="M20 12H5m6-6-6 6 6 6"/>',
    file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h6"/>',
    team: '<circle cx="9" cy="7" r="3"/><path d="M3 20v-3a6 6 0 0 1 12 0v3M17 4a3 3 0 0 1 0 6M21 20v-3a5 5 0 0 0-3-4"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    chat: '<path d="M21 11a8 8 0 0 1-8 8H8l-5 3 1-6a8 8 0 1 1 17-5Z"/><path d="M8 10h8M8 14h5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    play: '<path d="m9 5 11 7-11 7Z"/>',
  };
  const icon = (name) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.file}</svg>`;
  let store = { projects: [], active: null, tourSeen: false };
  let storageWorks = true;
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || "null");
    if (saved && Array.isArray(saved.projects)) store = { ...store, ...saved };
  } catch {
    storageWorks = false;
  }
  let page = "home",
    questionIndex = 0,
    viewedStage = null,
    toastTimer,
    timer;
  let lastOpener = null;
  const project = () => store.projects.find((p) => p.id === store.active);
  // Explicit scenario choice, not an AI classifier. Older v9 projects remain courses.
  const scenarioOf = (p = project()) => p?.scenario || "course";
  const scenarios = {
    course: { label: "Обучение и курс", roles: [
      ["Методист", "Связать цель обучения с практикой", "Структура программы и задания", "Новая предметная роль"],
      ["Исследователь аудитории", "Понять опыт и ограничения учеников", "Вопросы для проверки потребностей и нагрузки", "Специализация аналитика"],
      ["Редактор", "Сделать материалы последовательными и понятными", "Согласованный текст и замечания к ясности", "Новая предметная роль"],
    ]},
    book: { label: "Книга и материалы", roles: [
      ["Редактор", "Связать замысел, читателя и структуру книги", "Редакционная концепция и план глав", "Новая предметная роль"],
      ["Дизайнер", "Подобрать визуальный язык под формат чтения", "Требования к оформлению и образцу разворота", "Специализация дизайнера"],
      ["Специалист по подготовке издания", "Не упустить требования к передаче макета", "Чек-лист файлов, прав и технических проверок", "Новая предметная роль"],
    ]},
    business: { label: "Бизнес и новая идея", roles: [
      ["Исследователь рынка", "Проверить, кому и зачем нужно предложение", "Карта гипотез спроса и план интервью", "Специализация аналитика"],
      ["Финансовый аналитик", "Выявить неизвестные расходы и источники дохода", "Структура расчёта и список исходных данных", "Специализация аналитика"],
      ["Специалист по запуску", "Связать выводы в последовательность действий", "План первой проверки идеи и ограничения", "Новая предметная роль"],
    ]},
    other: { label: "Другая задача / пока не знаю", roles: [
      ["Аналитик задачи", "Отделить цель от предположений и неизвестного", "Карта вопросов и недостающих данных", "Специализация аналитика"],
      ["Исследователь решений", "Сравнить возможные подходы", "Варианты решения и основания выбора", "Специализация аналитика"],
      ["Редактор результата", "Объединить выводы в понятный документ", "Структура решения и следующий шаг", "Новая предметная роль"],
    ]},
  };
  function stagesFor(p = project()) {
    const type = scenarioOf(p);
    if (type === "course") return courseStages;
    const titles = type === "book" ? ["Концепция книги", "Редакционный план", "Бриф на образец и выпуск"]
      : type === "business" ? ["Карта гипотез идеи", "План проверки идеи", "Бриф на первый эксперимент"]
      : ["Карта задачи", "План решения", "Бриф на первый шаг"];
    const results = type === "book" ? ["Для кого книга, её замысел и открытые вопросы.", "Структура и порядок подготовки материалов.", "Требования к образцу и передаче в производство; не готовый печатный макет."]
      : type === "business" ? ["Что известно и что нужно проверить до вложений.", "Последовательность проверок спроса и ограничений.", "Условия теста, необходимые данные и критерии решения."]
      : ["Цель, ограничения и неизвестные.", "Варианты действий и порядок проверки.", "Что сделать первым и как оценить результат."];
    return courseStages.map((s, i) => ({...s, title: titles[i], short: ["Разбор", "План", "Первый шаг"][i], result: results[i], next: i < 2 ? `Перейти: ${titles[i + 1].toLowerCase()}` : "Принять итоговый результат"}));
  }
  function questionsFor(p = project()) {
    const type = scenarioOf(p);
    if (type === "course") return courseQuestions;
    const book = type === "book", business = type === "business";
    return [
      { title: "Что вы хотите получить в первую очередь?", help: "Можно выбрать несколько ответов и добавить свой.", options: book ? ["Определить замысел книги", "Собрать структуру", "Улучшить текст", "Подготовиться к изданию"] : business ? ["Проверить спрос", "Разобраться с расходами", "Спланировать запуск", "Продумать продвижение"] : ["Разобраться в проблеме", "Сравнить решения", "Получить план действий", "Подготовить материалы"], guide: "Начнём с нужного вам результата", copy: "По выбранному направлению я предложу роли и объясню их вклад. В демо это подготовленные схемы, а не автоматический анализ ИИ." },
      { title: book ? "Для кого вы пишете?" : "Кому должен помочь результат?", help: "Укажите аудиторию. Если пока не знаете — это тоже важная информация.", options: book ? ["Для широкой аудитории", "Для специалистов", "Для детей и родителей", "Пока не определено"] : ["Мне и моему проекту", "Моим клиентам", "Моим сотрудникам", "Пока не определено"], guide: "Не будем додумывать за вас", copy: "Неизвестное останется вопросом для проверки. Предположение не станет фактом только потому, что его предложила команда." },
      { title: "Какие границы важно соблюдать?", help: "Выберите несколько ограничений или опишите свои.", options: ["Ограниченный бюджет", "Фиксированный срок", "Сохранить мой замысел", "Использовать готовые материалы"], guide: "Вы определяете границы", copy: "Команда предложит порядок работы. До начала вы увидите роли, результаты, оценку времени и условную стоимость." },
    ];
  }
  const suggestionsFor = (p) => scenarioOf(p) === "course" ? courseSuggestions : ["Начать с карты задачи — подтвердить", "Аудитория пока не определена", "Границы требуют уточнения"];
  function teamFor(p) {
    const team = [...(scenarios[scenarioOf(p)] || scenarios.other).roles];
    if (scenarioOf(p) === "business" && p.answers[0].includes("Продумать продвижение"))
      team.push(["Маркетолог", "Вы выбрали продвижение: нужны каналы первых контактов", "Гипотезы каналов и сообщений для теста", "Новая предметная роль"]);
    return team;
  }
  const decisionOptions = (p) => scenarioOf(p) === "course" ? ["Да, если главное останется в курсе", "Нет, вся теория должна остаться", "Предложите оба варианта"] : ["Начать с небольшой проверки", "Сначала сравнить варианты", "Предложите подход и объясните почему"];
  const persist = () => {
    try {
      localStorage.setItem(KEY, JSON.stringify(store));
    } catch {
      storageWorks = false;
      toast(
        "Браузер не сохранил изменения. Скачайте результат перед закрытием.",
      );
    }
  };
  const note = (text) => `<p class="helper">${text}</p>`;
  const button = (text, action, primary = false, extra = "") => {
    const extraClass = extra.match(/class="([^"]*)"/)?.[1] || "";
    return `<button class="button${primary ? " primary" : ""} ${extraClass}" data-action="${action}" ${extra.replace(/class="[^"]*"/, "")}>${text}</button>`;
  };
  const tag = (text, color = "") =>
    `<span class="tag ${color}"><i class="pill-dot"></i>${text}</span>`;
  function toast(text) {
    clearTimeout(toastTimer);
    $("#toast").textContent = text;
    $("#toast").hidden = false;
    toastTimer = setTimeout(() => ($("#toast").hidden = true), 4500);
  }
  function guide(title, copy, art = "guide") {
    return `<aside class="guide-card"><img src="${media(art)}" alt="${art === "result" ? "Команда передаёт единый результат" : "Помощник с блокнотом из ролика CoreTeams"}"><div class="guide-copy"><span class="guide-role">Ваш помощник · фасилитатор</span><h3>${title}</h3><p>${copy}</p><button class="text-button" data-action="video" data-time="36">Посмотреть мою роль в видео ↗</button><div class="guide-note">Вы отвечаете за цель и решения.<br>Я — за вопросы и работу команды.</div></div></aside>`;
  }
  function flow(body, aside, step = 0, back = "home") {
    return `<div class="flow-page"><div class="flow-top"><button class="back" data-action="back" data-target="${back}">${icon("back")}Назад</button><div class="flow-progress"><span>Подготовка проекта</span>${[0, 1, 2, 3].map((n) => `<i class="${n <= step ? "on" : ""}"></i>`).join("")}</div></div><div class="flow-layout"><section class="flow-body">${body}</section>${aside}</div></div>`;
  }
  function newProject(example = false) {
    const unfinished = store.projects.find(p => p.status === "draft" && (example ? p.example : !p.example && !p.name.trim()));
    if(unfinished){store.active=unfinished.id;questionIndex=unfinished.questionIndex||0;persist();go(unfinished.setup||"welcome");return;}
    if (store.projects.length >= MAX_PROJECTS) {
      go("profile");
      toast(
        "В демо доступно 3 проекта. Можно продолжить один из существующих.",
      );
      return;
    }
    const p = {
      id: `p-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      example,
      scenario: example ? "course" : "other",
      name: example ? "Исследования для начинающих дизайнеров" : "",
      problem: example
        ? "Ученики долго изучают теорию и редко доходят до самостоятельного исследования. Хочу больше практики и понятный первый шаг."
        : "",
      context: example
        ? "Курс на 5 недель. Важно сохранить реальные кейсы автора. Ученики совмещают обучение с работой."
        : "",
      files: example
        ? [{ name: "Программа курса · пример.md", example: true }]
        : [],
      answers: [[], [], []],
      custom: ["", "", ""],
      questionIndex: 0,
      setup: "welcome",
      stage: 0,
      status: "draft",
      progress: 0,
      spent: 0,
      limit: 240,
      artifacts: [[], [], []],
      events: [],
      messages: [],
      awaitingAnswered: false,
      answer: [],
      answerCustom: "",
      feedback: "",
      feedbackNote: "",
      created: new Date().toISOString(),
      updated: new Date().toISOString(),
    };
    store.projects.push(p);
    store.active = p.id;
    persist();
    go(example ? "welcome" : "account");
  }
  function home() {
    return `<div class="page"><section class="home-hero"><div><p class="home-kicker"><span class="line"></span>AI-команда вокруг вашей задачи</p><h1 class="home-title">Ваша задача.<br>Нужная команда.<br><span>Один результат.</span></h1><p class="lead">Расскажите, что хотите сделать. CoreTeams подберёт специалистов, организует их работу и соберёт выводы в понятное решение.</p><div class="actions">${button("Рассказать о задаче " + icon("arrow"), "start", true)}${button("Посмотреть на примере", "example")}</div><p class="helper">Книга, учебный курс, бизнес-идея или другая задача.<br>Вы задаёте цель — мы предлагаем команду.</p></div><figure class="home-visual" style="margin:0"><img src="${media("team")}" alt="Помощник CoreTeams и команда специалистов в общей мастерской"><figcaption class="visual-caption"><div><p>Как задача становится результатом</p><small>Знакомство с CoreTeams · 75 секунд</small></div><button class="play-button" data-action="video" aria-label="Смотреть видео о CoreTeams">${icon("play")}</button></figcaption></figure></section><section class="home-how" aria-label="Как устроена работа"><article><span class="step-number">01</span><div><h3>Расскажите о задаче</h3><p>Помощник задаст вопросы и подскажет, какие материалы пригодятся.</p></div></article><article><span class="step-number">02</span><div><h3>Познакомьтесь с командой</h3><p>Почему нужны эти роли, за что отвечает каждая и что получится вместе.</p></div></article><article><span class="step-number">03</span><div><h3>Решите, что делать дальше</h3><p>Примите результат или попросите изменить. Проект можно продолжить позже.</p></div></article></section><p class="home-demo-note">Интерактивное демо: ответы команды подготовлены заранее. Ваши данные остаются в этом браузере.</p></div>`;
  }
  function account() {
    return flow(
      `<p class="eyebrow">Ваше пространство</p><h1>Чтобы вернуться к проекту</h1><p class="lead">В готовом сервисе проекты будут связаны с аккаунтом. Сейчас можно пройти сценарий без регистрации.</p><form id="account-form"><label class="field"><span>Почта</span><input name="email" type="email" autocomplete="email" placeholder="name@example.com" required></label><label class="field"><span>Пароль</span><input name="password" type="password" autocomplete="new-password" minlength="8" placeholder="Не менее 8 символов" required></label><p class="helper">Форма показывает будущий вход. Почта и пароль не отправляются и не сохраняются.</p><div class="actions"><button class="button primary" type="submit">Продолжить демо</button><button class="text-button" type="button" data-action="welcome">Пропустить регистрацию</button></div></form>`,
      guide(
        "Проект не потеряется",
        "Описание задачи, ответы и результаты сохраняются на этом устройстве. Сможете вернуться через «Мои проекты».",
      ),
      0,
    );
  }
  function welcome() {
    const p = project();
    return flow(
      `${p.example ? tag("Вы пробуете на готовом примере") : tag("Знакомство · меньше минуты")}<h1 class="spacer-top">Вам не нужно знать,<br>с чего начать</h1><p class="lead">Я помогу описать задачу и организую работу команды. Вы сможете остановиться, уточнить или вернуться позже.</p><ol class="welcome-points"><li><span class="step-number">01</span><div><b>Принесите то, что уже есть</b><p>Описание идеи, документы, заметки или просто вопрос.</p></div></li><li><span class="step-number">02</span><div><b>Ответьте на три вопроса</b><p>Выбирайте варианты, добавляйте свой ответ или попросите подсказку.</p></div></li><li><span class="step-number">03</span><div><b>Согласуйте команду и план</b><p>Покажу, какие роли нужны, что они подготовят и как соединят выводы.</p></div></li></ol>${button("Рассказать о задаче " + icon("arrow"), "context", true)}${note("Подготовка занимает около 3 минут. Кредиты пока не расходуются.")}`,
      guide(
        "Я буду вашим проводником",
        "Меня называют фасилитатором. Я уточняю цель, передаю контекст специалистам и возвращаюсь к вам с вопросами и результатами.",
      ),
      0,
    );
  }
  function filesList(p) {
    return p.files.length
      ? `<ul class="file-list">${p.files.map((f, i) => `<li>${esc(f.name)}<button type="button" data-action="remove-file" data-index="${i}" aria-label="Убрать ${esc(f.name)} из проекта">×</button></li>`).join("")}</ul>`
      : '<p class="helper">Файлов пока нет. Можно продолжить с описанием.</p>';
  }
  function upload(p) {
    return `<div class="upload-box"><label>${icon("plus")}Добавить файлы<input class="file-input" aria-label="Добавить файлы проекта" type="file" multiple data-files accept=".pdf,.doc,.docx,.ppt,.pptx,.txt,.md,.png,.jpg,.jpeg,.csv,.xlsx"></label><div data-file-list>${filesList(p)}</div><p class="helper">PDF, документы, презентации, изображения.<br>В демо сохраняются только названия, содержимое файлов не анализируется.</p></div>`;
  }
  function context() {
    const p = project();
    return flow(
      `<p class="eyebrow">1. Ваша задача</p><h1>Что вы хотите сделать?</h1><p class="lead">Начните с того, что знаете. Необязательно сразу описывать всё.</p>${p.example ? '<div class="notice">Это вымышленный курс для знакомства с сервисом. Любое поле можно изменить.</div>' : ""}<form id="context-form"><label class="field"><span>Какое направление ближе к задаче?</span><select name="scenario" id="scenario-choice">${Object.entries(scenarios).map(([key, value]) => `<option value="${key}" ${scenarioOf(p) === key ? "selected" : ""}>${value.label}</option>`).join("")}</select></label><p class="helper">Это подсказка для демо, не окончательная классификация. Если сомневаетесь, оставьте «Другая задача».</p><label class="field"><span>Название проекта</span><input name="name" value="${esc(p.name)}" placeholder="Например, моя кофейня, книга или учебный курс" required maxlength="120" data-project-field="name"></label><label class="field"><span>Что хочется изменить или понять?</span><textarea name="problem" placeholder="Например: хочу открыть кофейню, но не знаю, что проверить до аренды помещения." required maxlength="4000" data-project-field="problem">${esc(p.problem)}</textarea></label><details class="spacer-top" ${p.context ? "open" : ""}><summary>Добавить контекст: сроки, ограничения, аудитория</summary><label class="field"><span>Что ещё важно команде <span class="optional">· необязательно</span></span><textarea name="context" data-project-field="context" placeholder="Для кого проект, что уже сделано, что нельзя менять…" maxlength="5000">${esc(p.context)}</textarea></label></details>${upload(p)}<div class="actions"><button class="button primary" type="submit">К уточняющим вопросам ${icon("arrow")}</button></div></form>`,
      guide(
        "Хорошее начало — конкретный пример",
        "Подойдут черновик, расчёты, отзывы или заметки. Если материалов пока нет, начнём с описания и отметим, что ещё нужно проверить.",
      ),
      0,
      "welcome",
    );
  }
  function question() {
    const p = project(),
      q = questionsFor(p)[questionIndex];
    return flow(
      `<p class="eyebrow">2. Уточнение · вопрос ${questionIndex + 1} из 3</p><h1>${q.title}</h1><p class="lead">${q.help}</p><div class="choices" role="group" aria-label="Варианты ответа">${q.options.map((v, i) => `<button class="choice" data-action="answer" data-index="${i}" aria-pressed="${p.answers[questionIndex].includes(v)}"><span class="check">✓</span>${v}</button>`).join("")}</div><label class="field"><span>Свой ответ <span class="optional">· можно дополнить выбранное</span></span><input id="custom-answer" value="${esc(p.custom[questionIndex])}" placeholder="Напишите, что ещё важно" maxlength="1000"></label><div class="suggestion"><button class="text-button" data-action="suggest-answer">Не уверен — предложите вариант</button></div><div id="answer-suggestion">${p.answers[questionIndex].includes("Предложение команды") ? '<div class="notice">Начнём с предположения команды и явно отметим его в плане. Это можно изменить перед запуском.</div>' : ""}</div><div class="actions">${button(questionIndex < 2 ? "Следующий вопрос " + icon("arrow") : "Посмотреть команду " + icon("arrow"), "next-question", true, `id="next-question" ${answerReady() ? "" : "disabled"}`)}</div>`,
      guide(q.guide, q.copy),
      1,
      questionIndex === 0 ? "context" : "previous-question",
    );
  }
  const answerReady = () => {
    const p = project();
    return (
      p &&
      (p.answers[questionIndex].length > 0 || p.custom[questionIndex].trim())
    );
  };
  const answerText = (p, i) =>
    [
      ...p.answers[i].map((x) =>
        x === "Предложение команды" ? suggestionsFor(p)[i] : x,
      ),
      p.custom[i],
    ]
      .filter(Boolean)
      .join(", ") || "Нужно уточнить";
  function teamCards(p) {
    const team = teamFor(p);
    return `<div class="team-cards">${team.map((r, i) => `<article class="team-card"><div class="team-card-heading"><span class="role-symbol">${i + 1}</span><h3>${esc(r[0])}</h3></div><p><b>Зачем:</b> ${esc(r[1])}.</p><dl><div><dt>Результат роли</dt><dd>${esc(r[2])}.</dd></div><div><dt>Передаёт дальше</dt><dd>${i < team.length - 1 ? esc(team[i + 1][0]) + " использует выводы в своей части." : "Фасилитатору — для объединения и согласования с вами."}</dd></div></dl><small class="role-origin">${esc(r[3])} · предложение для проекта, не подключённый агент</small></article>`).join("")}</div>`;
  }
  function teamPlan() {
    const p = project();
    return flow(
      `<p class="eyebrow">3. Команда вокруг вашей задачи</p><h1>Вот кто поможет её разобрать</h1><p class="lead">Не нужно выбирать агентов или раздавать поручения. Посмотрите, зачем нужна каждая роль и что получится вместе.</p>
      <p class="helper">Для проекта «${esc(p.name)}» · ${esc(scenarios[scenarioOf(p)].label)}.<br>Ваш приоритет: ${esc(answerText(p, 0))}.</p>
      ${assistant("Данных достаточно для первого разбора. Предлагаю эти роли: я свяжу их работу и вернусь к вам с единым результатом. Есть что добавить?")}
      ${teamCards(p)}
      <section class="team-unified"><h3>Вместе — один результат</h3><p>${stagesFor(p).map(s => esc(s.title)).join(" → ")}. Выводы ролей дополняют друг друга и входят в общий результат проекта.</p></section>
      <details class="spacer-top"><summary>Откуда берутся роли, которых нет в CTF?</summary><p>CTF предусматривает подключение готовых ролей, специализацию под предметную область и добавление новой роли с инструкциями, знаниями, памятью и проверками. Одного названия специалиста недостаточно.</p><p>В этом прототипе команда предложена по выбранному направлению и ответам. Роли не создаются и не запускаются во фреймворке. Их подготовку и доступность должен подтвердить серверный исполнитель.</p></details>
      <div class="notice spacer-top">Демо: это пример подбора команды, а не подтверждение её технического подключения. Для сложных решений выводы ИИ потребуют проверки.</div>
      <div class="actions">${button("Команда подходит — к плану " + icon("arrow"), "plan", true)}${button("Обсудить состав", "discuss-team")}</div>`,
      guide("Я организую работу", "Передам цель и материалы каждой роли, свяжу их выводы и остановлюсь, если понадобится ваше решение. Вы обсуждаете задачу со мной, а не управляете агентами."),
      2, "previous-question"
    );
  }
  function plan() {
    const p = project();
    const assumptions = p.answers
      .map((a, i) =>
        a.includes("Предложение команды") ? suggestionsFor(p)[i] : null,
      )
      .filter(Boolean);
    return flow(
      `<p class="eyebrow">4. План и согласование</p><h1>Вот как мы поняли задачу</h1><p class="lead">Данных достаточно для первого разбора. Проверьте фокус и границы.</p>
      <div class="plan-summary"><h3>${esc(p.name)}</h3><dl class="spacer-top">
      <div><dt>Ваша цель</dt><dd>${esc(p.problem)}</dd></div>
      <div><dt>Фокус</dt><dd>${esc(answerText(p, 0))}</dd></div>
      <div><dt>Аудитория</dt><dd>${esc(answerText(p, 1))}</dd></div>
      <div><dt>Границы</dt><dd>${esc(answerText(p, 2))}</dd></div></dl>
      <details><summary>Контекст и материалы · ${p.files.length} файлов</summary><p>${esc(p.context || "Дополнительных ограничений нет")}</p><p>${p.files.map((f) => esc(f.name)).join(", ") || "Пока только описание"}</p></details>
      <button class="text-button" data-action="context">Изменить описание или добавить файлы</button></div>
      ${assumptions.length ? `<p class="helper">Требует проверки: ${assumptions.map(esc).join("; ")}.</p>` : ""}
      <div class="cost-box"><div><b>3–5 минут</b><small>до первого результата</small></div><div><b>до 180 кредитов</b><small>три этапа · лимит 240</small></div></div>
      <p class="helper">В демо этапы ускорены, списания условные. На паузе кредиты не расходуются.</p>
      ${p.teamFeedback ? `<div class="notice">Уточнение к команде: ${esc(p.teamFeedback)}<br>Сохранено; автоматический пересмотр состава в демо не выполняется.</div>` : ""}
      <label class="checkline"><input id="plan-confirm" type="checkbox">Да, задача, команда и ограничения указаны верно</label>
      <div class="actions">${button("Начать работу команды " + icon("arrow"), "start-run", true, 'id="start-run" disabled')}</div>`,
      `<aside class="plan-aside"><div class="guide-card"><img src="${media("team")}" alt="Команда из ролика CoreTeams"><div class="guide-copy"><span class="guide-role">Предложенная команда · демо</span><h3>${teamFor(p).map(r => esc(r[0])).join(", ")}</h3><p>Фасилитатор организует передачу результатов и согласование с вами.</p><button class="text-button" data-action="team-plan">Посмотреть ответственность ролей ↗</button></div></div><div class="plan-outcomes"><p class="side-label">Что вы получите</p><ol class="plan-timeline">${stagesFor(p).map((s, i) => `<li><span class="num">${i + 1}</span><div><h4>${s.title}</h4><p>${s.result}</p></div></li>`).join("")}</ol><p class="helper">Каждый результат согласуем с вами перед следующим этапом.</p></div></aside>`,
      3,
      "team-plan",
    );
  }
  function statusLabel(p) {
    return (
      {
        draft: "Подготовка",
        working: "Команда работает",
        waiting: "Нужен ваш ответ",
        review: "Проверьте результат",
        ready: "Все результаты приняты",
        done: "Проект завершён",
      }[p.status] || "Подготовка"
    );
  }
  function statusColor(p) {
    return p.status === "waiting"
      ? "amber"
      : ["ready", "done", "review"].includes(p.status)
        ? "green"
        : "";
  }
  function roles(p) {
    const team = teamFor(p);
    const active = p.status === "working" && p.progress >= 25
      ? Math.min(team.length - 1, Math.floor((p.progress - 25) / 75 * team.length)) : -1;
    return `<p class="helper">Симуляция · роли не подключены к CTF</p><div class="role-list">${team
      .map(
        (r, i) =>
          `<div class="role-row ${active === i ? "active" : ""}"><span class="role-symbol">${esc(r[0][0])}</span><div><b>${esc(r[0])}</b><small>${active === i ? "В работе по сценарию · " : ""}${esc(r[2])}</small></div></div>`,
      )
      .join("")}</div>`;
  }
  function eta(p) {
    if (p.status === "waiting")
      return "Оценка времени на паузе.<br>Кредиты не расходуются.";
    if (p.status === "review")
      return "Этап готов. Следующий начнётся<br>только после вашего одобрения.";
    if (["ready", "done"].includes(p.status)) return "Все три этапа завершены.";
    return `Ориентир: ${stagesFor(p)[p.stage].time} на этап.<br>В демо процесс ускорен.`;
  }
  function compactProgress(p) {
    return `<div class="status-heading">${tag(statusLabel(p),statusColor(p))}<strong>${p.progress}%</strong></div><div class="progress"><span style="width:${p.progress}%"></span></div><p class="eta">${eta(p)} · ${p.spent} / ${p.limit} демо-кредитов</p>`;
  }
  function statusPanel(p) {
    return `<h3>Работа команды</h3>${tag(statusLabel(p), statusColor(p))}<div class="status-heading"><span>Этап ${p.stage + 1} из 3</span><strong>${p.progress}%</strong></div><div class="progress" role="progressbar" aria-label="Готовность текущего этапа" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${p.progress}"><span style="width:${p.progress}%"></span></div><p class="eta">${eta(p)}</p><section class="status-section"><p class="side-label">Кто помогает</p>${roles(p)}</section><section class="status-section"><p class="side-label">Кредиты проекта · демо</p><div class="credit-line"><span>Использовано</span><b>${p.spent} из ${p.limit}</b></div><div class="credit-line"><span>Осталось</span><b>${p.limit - p.spent}</b></div><p class="eta">Подготовка и ожидание ответа бесплатны.</p></section><details class="status-section"><summary>Что уже сделано</summary><ul>${
      p.events
        .slice(-6)
        .map((e) => `<li>${esc(e)}</li>`)
        .join("") || "<li>Команда получила описание задачи.</li>"
    }</ul></details>`;
  }
  function assistant(text) {
    return `<div class="assistant-message"><span class="assistant-avatar">${icon("chat")}</span><div><div class="speaker">Помощник <small>фасилитатор команды</small></div><p>${text}</p></div></div>`;
  }
  function workSteps(p) {
    const team = teamFor(p);
    const tasks = [
      ["Уточняем общее направление", p.stage === 0 ? "Фасилитатор передаёт цель, контекст и границы" : "Фасилитатор передаёт одобренный результат предыдущего этапа"],
      ...team.map((r, i) => [r[0], r[2] + (i ? " · с учётом выводов предыдущей роли" : " · на основе контекста проекта")]),
    ];
    const active = p.progress < 25 ? 0 : Math.min(tasks.length - 1, 1 + Math.floor((p.progress - 25) / 75 * team.length));
    return `<ul class="work-steps">${tasks.map((x, i) => `<li class="${p.progress === 100 || i < active ? "done" : i === active ? "active" : ""}"><span class="state-icon">${p.progress === 100 || i < active ? "✓" : i === active ? "◉" : "○"}</span><div>${esc(x[0])}<small>${esc(x[1])}</small></div></li>`).join("")}</ul><p class="helper">Фасилитатор собирает части в единый результат и передаёт его вам на согласование. Ход работы в демо ускорен и имитируется.</p>`;
  }
  function waiting(p) {
    const course = scenarioOf(p) === "course";
    const message = course ? "Методист заметил развилку: можно сократить теорию или сохранить её целиком и изменить порядок. Уточню ваше решение." : "Команда дошла до развилки. Можно сначала проверить одну ключевую гипотезу или сравнить несколько подходов. Прежде чем продолжить, уточню ваш выбор.";
    return `${assistant(message)}<section class="phase-callout"><span class="tag amber">Нужен ваш ответ · работа на паузе</span><h3>${course ? "Можно ли перенести часть теории в дополнительные материалы?" : "С какого подхода начнём?"}</h3><p>${course ? "Это освободит место для практики." : "Небольшая проверка быстрее даст обратную связь; сравнение расширит выбор, но потребует больше данных."} Выберите вариант или уточните своими словами.</p><div class="choices" role="group" aria-label="Решение для продолжения">${decisionOptions(p).map((x, i) => `<button class="choice" data-action="decision" data-index="${i}" aria-pressed="${p.answer.includes(x)}"><span class="check">✓</span>${x}</button>`).join("")}</div><label class="field"><span>Добавить уточнение</span><input id="decision-custom" value="${esc(p.answerCustom)}" placeholder="Что важно учесть при выборе?" maxlength="1000"></label><div class="actions">${button("Ответить и продолжить", "resume", true, `id="resume" ${p.answer.length || p.answerCustom.trim() ? "" : "disabled"}`)}</div><p class="helper">Пока вы решаете, время и кредиты не расходуются.</p></section>`;
  }
  function currentArtifact(p, stage = p.stage) {
    return p.artifacts[stage]?.at(-1);
  }
  function renderDocument(doc) {
    return `<div class="paper"><div class="paper-top"><span>РЕЗУЛЬТАТ КОМАНДЫ · ВЕРСИЯ ${doc.version}</span><span>${doc.accepted ? "Одобрен" : "Для вашего решения"}</span></div><div class="paper-body"><h3>${esc(doc.title)}</h3>${doc.sections.map((s) => `<section class="finding"><h4>${esc(s.heading)}</h4>${s.paragraph ? `<p>${esc(s.paragraph)}</p>` : ""}${s.items ? `<ul>${s.items.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}</section>`).join("")}</div></div>`;
  }
  function review(p, stage) {
    const doc = currentArtifact(p, stage);
    if (!doc) return "";
    const active = stage === p.stage && p.status === "review";
    return `${assistant(active ? "Вариант готов. Посмотрите, подходит ли предложенная логика. Можно принять результат или описать, что изменить." : "Это сохранённая версия результата. Она остаётся доступной вместе с проектом.")}${renderDocument(doc)}${
      p.artifacts[stage].length > 1
        ? `<details class="spacer-top"><summary>Предыдущие версии · ${p.artifacts[stage].length - 1}</summary>${p.artifacts[
            stage
          ]
            .slice(0, -1)
            .map(
              (d) =>
                `<button class="text-button" data-action="version" data-stage="${stage}" data-version="${d.version}">Открыть версию ${d.version}</button><br>`,
            )
            .join("")}</details>`
        : ""
    }${active ? `<section class="review-actions"><h3>Подходит ли вам этот результат?</h3><p>${stage < 2 ? "Если всё верно, одобрите его. После этого команда начнёт следующий этап." : "Примите результат, чтобы завершить проект или продолжить его развитие."}</p><div class="actions">${button(stagesFor(p)[stage].next + " " + icon("arrow"), "approve", true)}${button("Нужно изменить", "show-revision")}</div></section>` : ""}<div id="revision-slot"></div><div class="actions">${button(icon("download") + " Скачать этот результат", "download-one", false, `data-stage="${stage}"`)}</div>`;
  }
  function complete(p) {
    return `<div class="completion-hero">${icon("check")}<div><h3>Проект готов к следующему шагу</h3><p>Три результата собраны и одобрены вами.</p></div></div>${assistant(scenarioOf(p) === "course" ? "Теперь можно проверить первый модуль на небольшой группе учеников. Материалы сохранены в проекте." : "У вас есть заготовка плана для проверки. Сверьте её с реальными данными: демо не проводило исследование и не проверяло файлы. Материалы сохранены в проекте.")}<div class="choices">${stagesFor(p).map((s, i) => `<button class="choice" data-action="open-artifact" data-stage="${i}">${icon("file")}<div>${s.title}<small>Одобрено · версия ${currentArtifact(p, i)?.version || 1}</small></div><span style="margin-left:auto">↗</span></button>`).join("")}</div><div class="actions">${button(icon("download") + " Скачать всё одним файлом", "download-all", true)}</div><p class="export-note">Markdown (.md): результаты, ваши решения и контекст проекта.</p><section class="review-actions"><h3>Помог ли результат сделать следующий шаг?</h3><p>Ответ необязателен. Он сохранится в этом демо.</p><div class="feedback-row" role="group" aria-label="Оценка результата">${[
      ["yes", "Да, могу действовать"],
      ["partly", "Нужна доработка"],
      ["no", "Не помог"],
    ]
      .map(
        ([v, t]) =>
          `<button class="choice" data-action="feedback" data-value="${v}" aria-pressed="${p.feedback === v}">${t}</button>`,
      )
      .join(
        "",
      )}</div><label class="field"><span>Что стоит улучшить? <span class="optional">· необязательно</span></span><textarea id="feedback-note" placeholder="Ваш комментарий" maxlength="2000">${esc(p.feedbackNote)}</textarea></label><p class="helper" id="feedback-state">${p.feedback ? "Спасибо, оценка сохранена." : "Можно оставить отзыв или сразу завершить проект."}</p></section><div class="actions">${button(p.status === "done" ? "Вернуться в мои проекты" : "Завершить и открыть мои проекты", "finish", true)}${button("Продолжить доработку", "reopen")}</div><details class="spacer-top"><summary>Развитие проекта: масштабирование и инвестор</summary><p>Эти направления пока не подключены. Можно сохранить пожелание как следующий шаг проекта — публикации не будет.</p>${button("Масштабировать проект", "next-step", false, 'data-value="Масштабировать проект"')}${button("Подготовить проект для инвестора", "next-step", false, 'data-value="Подготовить проект для инвестора"')}</details>`;
  }
  function workspace() {
    const p = project();
    if (!p) return profile();
    const stage = viewedStage ?? p.stage;
    let body;
    if (viewedStage !== null && currentArtifact(p, stage))
      body =
        review(p, stage) +
        (p.status === "ready" || p.status === "done"
          ? `<div class="actions">${button("К итогам проекта", "show-results", true)}</div>`
          : "");
    else if (["ready", "done"].includes(p.status)) body = complete(p);
    else if (p.status === "waiting") body = waiting(p);
    else if (p.status === "review") body = review(p, p.stage);
    else
      body = `${assistant(p.revising ? "Передаю ваше замечание команде. Предыдущая версия сохранена; новый вариант появится здесь." : `Команда работает по согласованному плану: ${teamFor(p).map(r => r[0].toLowerCase()).join(" → ")}. Я передаю контекст между ролями и возвращаю вам единый результат. Это демонстрация процесса, не запуск CTF.`)}<div id="work-steps">${workSteps(p)}</div><p class="quiet-note">Можно заниматься своими делами. Если понадобится решение, вопрос появится здесь. В демо работа продолжится при возвращении на страницу.</p>`;
    return `<div class="workspace"><header class="workspace-top"><div><p class="eyebrow">Ваш проект${p.example ? " · учебный пример" : ""}</p><h1>${esc(p.name)}</h1><p class="saved">${storageWorks ? "Сохранено на этом устройстве" : "Хранилище недоступно — скачайте результат"} · <a href="#profile">Мои проекты ↗</a></p></div><div class="workspace-tools">${button(icon("plus") + " Материалы и контекст", "materials")}${button(icon("team") + " Команда", "team", false, 'class="button"')}</div></header><div class="workspace-grid"><aside class="project-side"><p class="side-label">Путь проекта</p><ol class="stage-nav">${stagesFor(p).map((s, i) => `<li><button data-action="stage" data-stage="${i}" class="${stage === i ? "active" : ""}" ${i > p.stage ? "disabled" : ""} ${stage === i ? 'aria-current="step"' : ""}><span class="stage-marker">${currentArtifact(p, i)?.accepted ? "✓" : String(i + 1).padStart(2, "0")}</span><span>${s.short}<small>${i > p.stage ? "Следом" : i < p.stage || ["ready", "done"].includes(p.status) ? "Одобрено" : statusLabel(p)}</small></span></button></li>`).join("")}</ol><div class="side-summary"><p class="side-label">Зачем работаем</p><p>${esc(p.problem)}</p></div><section class="side-materials"><p class="side-label">Память проекта</p><p class="side-summary">${p.files.length} файлов<br>${p.context ? "Контекст добавлен" : "Можно добавить контекст"}</p><button class="text-button" data-action="materials">Открыть материалы ↗</button></section></aside><section class="working-main">${!store.tourSeen ? `<div class="workspace-tour"><div><b>Вы в рабочем пространстве.</b> Здесь появятся вопросы и результаты. Этапы — слева, работа команды — справа. На узком экране откройте кнопку «Команда».</div><button aria-label="Закрыть подсказку" data-action="dismiss-tour">×</button></div>` : ""}<p class="eyebrow">${["ready", "done"].includes(p.status) && viewedStage === null ? "Все этапы пройдены" : `Этап ${stage + 1} из 3`}</p><h2>${["ready", "done"].includes(p.status) && viewedStage === null ? "Единый результат" : stagesFor(p)[stage].title}</h2><div id="work-content">${body}</div>${p.contextChanged ? '<p class="context-update">Контекст обновлён. Команда учтёт его в следующем результате или доработке.</p>' : ""}<div class="conversation-history">${p.messages
      .slice(-4)
      .map(
        (m) =>
          `<div class="conversation-entry"><small>${m.role === "user" ? "Вы" : "Помощник"}</small>${esc(m.text)}</div>`,
      )
      .join(
        "",
      )}</div><form id="message-form" class="composer"><label for="message">Есть уточнение к задаче?</label><div class="composer-row"><textarea id="message" name="message" rows="1" placeholder="Напишите, что команде важно учесть…" maxlength="3000" required>${esc(p.messageDraft || "")}</textarea><button class="button" type="submit" aria-label="Передать уточнение помощнику">${icon("arrow")}</button></div><button class="text-button" type="button" data-action="materials">+ Добавить файл или контекст</button></form></section><aside class="status-panel" id="status-panel">${statusPanel(p)}</aside></div></div>`;
  }
  function profile() {
    return `<div class="page"><div class="profile-top"><div><p class="eyebrow">Ваше пространство</p><h1>Мои проекты</h1><p class="quota">${store.projects.length} из ${MAX_PROJECTS} проектов · демонстрационный лимит</p></div>${button(icon("plus") + " Новый проект", "start", true, store.projects.length >= MAX_PROJECTS ? "disabled" : "")}</div>${store.projects.length ? `<div class="project-list">${store.projects.map((p) => `<article class="project-card"><div>${tag(statusLabel(p), statusColor(p))}<h3>${esc(p.name || "Новый проект")}</h3><p>${p.status === "draft" ? "Описание и подготовка задачи" : `Этап ${p.stage + 1} из 3 · ${stagesFor(p)[p.stage].title}`}${p.example ? " · учебный пример" : ""}</p></div>${button(["done", "ready"].includes(p.status) ? "Открыть результат" : "Продолжить с этого места", "open-project", true, `data-id="${p.id}"`)}</article>`).join("")}</div>` : `<div class="empty"><span class="empty-icon">${icon("file")}</span><h3>Здесь появится ваш первый проект</h3><p>Можно принести свою задачу или пройти готовый пример, чтобы познакомиться с процессом.</p>${button("Попробовать на примере", "example", true)}</div>`}<p class="helper spacer-top">Проекты хранятся в этом браузере. Скачайте результаты, если хотите перенести их на другое устройство. Тарифы и оплата в демо не подключены.</p>${store.projects.length >= MAX_PROJECTS ? '<div class="notice">Достигнут лимит демо. Все созданные проекты доступны для продолжения и скачивания.</div>' : ""}</div>`;
  }
  function catalog() {
    return `<div class="page"><p class="eyebrow">Каким бывает результат</p><h1>Одна задача. Несколько точек зрения.</h1><p class="lead">Учебные примеры, чтобы понять формат работы. Это не реальные отзывы клиентов.</p><div class="catalog-grid"><article class="catalog-card"><img class="cover" src="${media("result")}" alt="Команда передаёт собранный документ"><div class="catalog-content"><p class="eyebrow">Образование · интерактивный пример</p><h3>Из теории — в практику</h3><p>Карта проблем курса, новая программа и первое занятие для начинающих дизайнеров.</p>${button("Пройти этот пример", "example", true)}</div></article><article class="catalog-card"><img class="cover book" src="${media("book")}" alt="Разворот демонстрационной книги Город между строк"><div class="catalog-content"><p class="eyebrow">Книга · пример результата</p><h3>«Город между строк»</h3><p>Рукопись, редакционный план и оформление книжного разворота.</p>${button("Посмотреть результат", "book-case")}</div></article><article class="catalog-card"><div class="cover cover-type" aria-hidden="true">☕</div><div class="catalog-content"><p class="eyebrow">Малый бизнес · будущий сценарий</p><h3>Кофейня у университета</h3><p>Оценка идеи через спрос, экономику, продвижение и ограничения запуска.</p>${button("Что подготовит команда", "coffee-case")}</div></article></div></div>`;
  }
  function render(focus = true) {
    const p = project();
    if (
      [
        "context",
        "welcome",
        "account",
        "question",
        "plan",
        "team-plan",
        "workspace",
      ].includes(page) &&
      !p
    )
      page = "home";
    const views = {
      home,
      account,
      welcome,
      context,
      question,
      plan,
      "team-plan": teamPlan,
      workspace,
      profile,
      catalog,
    };
    $("#main").innerHTML = (views[page] || home)();
    if(page === "workspace") {
      const compact=document.createElement("div");
      compact.id="compact-status";
      compact.className="compact-status";
      compact.innerHTML=compactProgress(p);
      $("#work-content").before(compact);
    }
    document.querySelectorAll("[data-nav]").forEach((a) => {
      if (a.dataset.nav === page) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    if (focus) {
      window.scrollTo({ top: 0 });
      $("#main").focus({ preventScroll: true });
    }
  }
  function go(next) {
    page = next;
    const p = project();
    if (p && ["welcome", "context", "question", "team-plan", "plan"].includes(next)) {
      p.setup = next;
      p.questionIndex = questionIndex;
      persist();
    }
    const hash = ["home", "catalog", "profile"].includes(next)
      ? `#${next}`
      : `#project/${p?.id}/${next}${next === "question" ? "/" + questionIndex : ""}`;
    if (location.hash !== hash) history.pushState(null, "", hash);
    render();
  }
  function readRoute() {
    const parts = location.hash.slice(1).split("/");
    if (["home", "catalog", "profile"].includes(parts[0])) {
      page = parts[0];
      return;
    }
    if (parts[0] === "project") {
      const p = store.projects.find((x) => x.id === parts[1]);
      if (p) {
        store.active = p.id;
        questionIndex = Math.max(
          0,
          Math.min(2, parts[3] !== undefined ? Number(parts[3]) || 0 : p.questionIndex || 0),
        );
        viewedStage = null;
        page =
          p.status !== "draft"
            ? "workspace"
            : ["account", "welcome", "context", "question", "team-plan", "plan"].includes(
                  parts[2],
                )
              ? parts[2]
              : "context";
        return;
      }
    }
    page = "home";
  }
  function openDialog(title, content) {
    lastOpener = document.activeElement;
    $("#dialog-body").innerHTML =
      `<header class="dialog-head"><h2 id="dialog-title">${title}</h2><button class="close-button" data-action="close-dialog" aria-label="Закрыть окно">×</button></header><div class="dialog-content">${content}</div>`;
    if (!$("#dialog").open) $("#dialog").showModal();
  }
  function closeDialog() {
    const v = $("video", $("#dialog"));
    if (v) v.pause();
    $("#dialog").close();
    if (lastOpener?.isConnected) lastOpener.focus();
  }
  function event(p, text) {
    p.events.push(text);
    p.updated = new Date().toISOString();
  }
  function startRun() {
    const p = project();
    if (p.status !== "draft" || !$("#plan-confirm")?.checked) return;
    p.stage = 0;
    p.status = "working";
    p.progress = 0;
    p.runBase = p.spent;
    p.runCost = stagesFor(p)[0].cost;
    event(p, "Согласован состав: " + teamFor(p).map(r => r[0]).join(", ") + ". Роли демонстрационные.");
    event(p, "Описание, ответы и границы переданы команде.");
    viewedStage = null;
    persist();
    go("workspace");
    startTimer();
  }
  function genericSections(p) {
    const type = scenarioOf(p);
    const steps = type === "book" ? [
      ["Опишите читателя и обещание книги.", "Соберите оглавление и один черновой фрагмент.", "Уточните формат: электронная книга, печать или оба."],
      ["Согласуйте структуру глав и что читатель получает в каждой.", "Подготовьте образец текста и соберите отзывы.", "Зафиксируйте требования к иллюстрациям и оформлению."],
      ["Передайте редактору черновую главу и критерии правки.", "Согласуйте с дизайнером формат, сетку и образец разворота.", "Уточните требования типографии и проверьте права на материалы. Готовый IDML в демо не создаётся."],
    ] : type === "business" ? [
      ["Опишите предполагаемого клиента и его задачу.", "Отделите сведения о спросе от предположений.", "Составьте перечень неизвестных расходов и ограничений запуска."],
      ["Поговорите с потенциальными клиентами и зафиксируйте наблюдения.", "Соберите реальные предложения по ключевым расходам.", "Сравните небольшой тест с полномасштабным запуском."],
      ["Сформулируйте одно предложение для проверки спроса.", "Согласуйте бюджет, срок и измеримый критерий теста.", "По результатам решите: продолжить, изменить предложение или остановиться. Демо не рассчитывает окупаемость."],
    ] : [
      ["Уточните, какое решение должен позволить принять результат.", "Отделите известные факты от предположений.", "Соберите недостающие материалы и ограничения."],
      ["Опишите минимум два способа решения.", "Сравните необходимые данные, ресурсы и риски.", "Выберите обратимый первый шаг и критерий проверки."],
      ["Назовите действие, исполнителя и ожидаемый результат.", "Определите, какие данные покажут успех или ошибку.", "После проверки пересмотрите план, прежде чем расширять работу."],
    ];
    return [
      {heading: "Рабочая заготовка, не выполненное исследование", paragraph: "Ниже — шаблон для выбранного направления. Команда ИИ ещё не подключена; факты о вашем проекте, рынке или материалах не проверялись."},
      {heading: stagesFor(p)[p.stage].title, items: steps[p.stage]},
      {heading: "Приоритет и границы", items: [answerText(p, 0), answerText(p, 2)]},
      {heading: "Ваш выбор во время работы", paragraph: [...p.answer, p.answerCustom].filter(Boolean).join(". ")},
      {heading: "Ответственность команды", items: teamFor(p).map(r => r[0] + ": " + r[2] + ".")},
      {heading: "До практического использования", paragraph: "Заполните заготовку проверенными данными. Примите решение о следующем шаге после проверки предположений."},
    ];
  }
  function buildArtifact(p) {
    const stage = p.stage;
    const preserve = answerText(p, 2);
    const audience = answerText(p, 1);
    const practical = p.answers[0].includes("Больше практики");
    const keepTheory = p.answer.includes("Нет, вся теория должна остаться");
    const sections = [
      { heading: "Задача автора", paragraph: p.problem },
      {
        heading: "На чём основан этот вариант",
        paragraph: `Ваше описание и ответы. Аудитория: ${audience.replace(/[.!?]+$/, "")}. Контекст: ${(p.context || "Дополнительный контекст не задан").replace(/[.!?]+$/, "")}. Это подготовленный пример для прототипа; файлы не анализировались.`,
      },
    ];
    if (scenarioOf(p) !== "course") sections.push(...genericSections(p));
    if (scenarioOf(p) === "course" && stage === 0)
      sections.push(
        {
          heading: "01 / Что проверить в первую очередь",
          items: [
            practical
              ? "Практика: проверить, встречает ли ученик полезное задание уже на первом занятии."
              : "Логика обучения: проверить связь между обещанным результатом курса и заданиями.",
            `Приоритеты автора: ${answerText(p, 0)}. Их нужно сверить с программой и отзывами учеников.`,
            "Нагрузка: выяснить, сколько времени ученики реально тратят на задания. Без этих данных нельзя уверенно назвать причину отсева.",
          ],
        },
        {
          heading: "02 / Предлагаемая концепция",
          paragraph: keepTheory
            ? "Сохранить всю теорию, но распределить её небольшими блоками непосредственно перед практикой. Каждый модуль заканчивается проверяемым действием ученика."
            : "Строить курс вокруг небольшого проекта ученика. Основные понятия давать перед заданием, дополнительные объяснения — отдельным материалом. Это предложение нужно проверить на программе курса.",
        },
        { heading: "03 / Что сохраняем", paragraph: preserve },
        {
          heading: "Решение автора во время разбора",
          paragraph:
            [...p.answer, p.answerCustom].filter(Boolean).join(". ") ||
            "Решение ещё не получено",
        },
        {
          heading: "Что остаётся гипотезой",
          items: [
            "Причины отсева и перегрузки не подтверждены данными.",
            "Нужны актуальная программа, примеры работ и обратная связь хотя бы нескольких учеников.",
          ],
        },
      );
    if (scenarioOf(p) === "course" && stage === 1)
      sections.push(
        {
          heading: "Новая логика программы",
          paragraph:
            "Ниже — предлагаемый каркас для учебного примера. Названия и содержание предстоит сверить с вашей предметной областью.",
        },
        {
          heading: "Модули и результаты",
          items: [
            "1. Поставить задачу: ученик формулирует один вопрос и объясняет, почему он важен.",
            "2. Подобрать способ проверки: выбирает метод и описывает, какие данные нужны.",
            "3. Провести пробу: выполняет небольшое исследование на реальном примере.",
            "4. Сделать вывод: отделяет наблюдения от предположений и ограничений.",
            "5. Собрать итог: представляет результат и план следующего действия.",
          ],
        },
        {
          heading: "Ограничения, которые учитываем",
          paragraph: `Сохранить: ${preserve}. ${keepTheory ? "Теория остаётся в основной программе." : "Дополнительные объяснения можно вынести за пределы основного пути, после согласования."}`,
        },
        {
          heading: "Проверка программы",
          items: [
            "У каждого модуля есть конкретный результат и критерий проверки.",
            "Нагрузка и длительность пока требуют проверки на реальных материалах.",
          ],
        },
      );
    if (scenarioOf(p) === "course" && stage === 2)
      sections.push(
        {
          heading: "Первое занятие / От темы к вопросу",
          paragraph:
            "Результат ученика: сформулировать проверяемый вопрос для собственного небольшого проекта.",
        },
        {
          heading: "Сценарий занятия",
          items: [
            "5 минут: разобрать пример расплывчатой задачи и конкретного вопроса.",
            "10 минут: выделить цель, аудиторию и то, что пока неизвестно.",
            "15 минут: написать собственный вопрос и выбрать способ проверки.",
            "10 минут: получить обратную связь по трём критериям и уточнить формулировку.",
          ],
        },
        {
          heading: "Задание для ученика",
          paragraph:
            "Опишите ситуацию в трёх предложениях. Сформулируйте один вопрос, ответ на который поможет принять решение. Укажите, какие сведения вам для этого понадобятся.",
        },
        {
          heading: "Критерии проверки",
          items: [
            "Из вопроса понятно, какое решение предстоит принять.",
            "Ответ можно получить наблюдением, исследованием или небольшой проверкой.",
            "Задача помещается в ограничения курса.",
          ],
        },
        {
          heading: "Перед запуском",
          paragraph: `Сверьте пример с вашей областью и проверьте занятие на 3–5 учениках. Аудитория: ${audience}. Это учебная заготовка, а не подтверждённая программа.`,
        },
      );
    if (p.revisionRequest)
      sections.push({
        heading: "Доработка по вашему запросу",
        paragraph: `Ваше замечание: ${p.revisionRequest.replace(/[.!?]+$/, "")}. В этом демо оно зафиксировано в новой версии; содержательная переработка требует подключения команды.`,
      });
    if (p.messages.some((m) => m.role === "user"))
      sections.push({
        heading: "Дополнительные уточнения автора",
        items: p.messages
          .filter((m) => m.role === "user")
          .slice(-5)
          .map((m) => m.text),
      });
    return {
      title: stagesFor(p)[stage].title,
      version: p.artifacts[stage].length + 1,
      sections,
      accepted: false,
      created: new Date().toISOString(),
    };
  }
  function tick() {
    const p = project();
    if (!p || p.status !== "working") return;
    p.progress = Math.min(100, p.progress + 7);
    p.spent = Math.min(
      p.limit,
      p.runBase + Math.floor((p.runCost * p.progress) / 100),
    );
    if (
      p.stage === 0 &&
      !p.awaitingAnswered &&
      p.progress >= 42 &&
      !p.revising
    ) {
      p.progress = 42;
      p.status = "waiting";
      event(p, "Команда ждёт вашего решения. Работа и списание на паузе.");
      persist();
      if (page === "workspace") render(false);
      toast("Команде нужен ваш ответ. Работа приостановлена.");
      return;
    }
    if (p.progress === 100) {
      p.artifacts[p.stage].push(buildArtifact(p));
      p.status = "review";
      p.revising = false;
      p.contextChanged = false;
      event(p, `${stagesFor(p)[p.stage].title}: новая версия готова для проверки.`);
      persist();
      if (page === "workspace") {
        viewedStage = null;
        render(false);
      }
      toast("Результат готов. Посмотрите и решите, что делать дальше.");
      return;
    }
    persist();
    if (page === "workspace") {
      const panel = $("#status-panel");
      if (panel) panel.innerHTML = statusPanel(p);
      const compact = $("#compact-status");
      if(compact)compact.innerHTML=compactProgress(p);
      const steps = $("#work-steps");
      if (steps) steps.innerHTML = workSteps(p);
    }
  }
  function startTimer() {
    if (!timer) timer = setInterval(tick, 850);
  }
  function approve() {
    const p = project(),
      doc = currentArtifact(p);
    if (p.status !== "review" || !doc) return;
    doc.accepted = true;
    event(p, `Автор одобрил: ${doc.title}.`);
    viewedStage = null;
    if (p.stage === 2) p.status = "ready";
    else {
      const next = p.stage + 1;
      if (p.spent + stagesFor(p)[next].cost > p.limit) {
        doc.accepted = false;
        toast(
          "Лимита не хватит на следующий этап. Текущий результат доступен для скачивания.",
        );
        persist();
        return;
      }
      p.stage = next;
      p.status = "working";
      p.progress = 0;
      p.runBase = p.spent;
      p.runCost = stagesFor(p)[next].cost;
      p.revisionRequest = "";
    }
    persist();
    render();
    startTimer();
  }
  function revisionForm() {
    const p = project();
    $("#revision-slot").innerHTML =
      `<form id="revision-form" class="revision-box"><h3>Что изменить в этом результате?</h3><label class="field"><span>Ваше замечание</span><textarea name="revision" required maxlength="3000" placeholder="Опишите, что не подходит и какого изменения вы ждёте">${esc(p.revisionDraft || "")}</textarea></label><p class="helper">Предыдущая версия сохранится. Доработка — 20 демо-кредитов, доступно ${p.limit - p.spent}.</p><div class="actions"><button class="button primary" type="submit" ${p.limit - p.spent < 20 ? "disabled" : ""}>Подготовить новую версию</button><button type="button" class="text-button" data-action="cancel-revision">Отмена</button></div>${p.limit - p.spent < 20 ? '<p class="error">Лимит исчерпан. Скачайте текущую версию; списаний не будет.</p>' : ""}</form>`;
    $("#revision-slot textarea").focus();
  }
  function markdown(p, docs) {
    return `# ${p.name}\n\nДемонстрационный результат CoreTeams. Содержимое файлов не анализировалось.\n\n## Контекст автора\n\n${p.problem}\n\n${p.context}\n\nМатериалы: ${p.files.map((f) => f.name).join(", ") || "не добавлены"}\n\n${docs.map((d) => `# ${d.title} — версия ${d.version}\n\n${d.sections.map((s) => `## ${s.heading}\n\n${s.paragraph || ""}${s.items ? "\n" + s.items.map((t) => "- " + t).join("\n") : ""}`).join("\n\n")}`).join("\n\n---\n\n")}`;
  }
  function download(p, docs) {
    const blob = new Blob(["\ufeff" + markdown(p, docs)], {
        type: "text/markdown;charset=utf-8",
      }),
      url = URL.createObjectURL(blob),
      a = document.createElement("a");
    a.href = url;
    a.download = `CoreTeams-${p.name.replace(/[<>:"/\\|?*]/g, "").slice(0, 60)}.md`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Подготовлен файл с результатом проекта.");
  }
  function materials() {
    const p = project();
    openDialog(
      "Материалы и контекст",
      `<p>Всё важное для команды — в одном месте. Дополнения относятся только к проекту «${esc(p.name || "Новый проект")}».</p>${upload(p)}<form id="materials-form"><label class="field"><span>Контекст проекта</span><textarea name="context" rows="6" maxlength="8000" placeholder="Ограничения, договорённости, важные детали">${esc(p.context)}</textarea></label><p class="helper">Уже одобренные версии не меняются. Дополнения попадут в следующий результат или доработку.</p><div class="actions"><button type="submit" class="button primary">Сохранить контекст</button></div></form>`,
    );
  }
  document.addEventListener("click", (e) => {
    const nav = e.target.closest("a[href]");
    if(nav?.getAttribute("href")==="#main"){e.preventDefault();$("#main").focus();return;}
    if (
      nav &&
      ["#home", "#catalog", "#profile"].includes(nav.getAttribute("href"))
    ) {
      e.preventDefault();
      go(nav.getAttribute("href").slice(1));
      return;
    }
    const b = e.target.closest("[data-action]");
    if (!b || b.disabled) return;
    const a = b.dataset.action,
      p = project();
    if (a === "start") return newProject(false);
    if (a === "example") return newProject(true);
    if (["welcome", "context", "team-plan", "plan"].includes(a)) return go(a);
    if (a === "back") {
      if (b.dataset.target === "previous-question") {
        questionIndex = ["plan", "team-plan"].includes(page) ? 2 : Math.max(0, questionIndex - 1);
        return go("question");
      }
      return go(b.dataset.target);
    }
    if (a === "answer") {
      const v = questionsFor(p)[questionIndex].options[Number(b.dataset.index)],
        ans = p.answers[questionIndex];
      p.answers[questionIndex] = ans.includes(v)
        ? ans.filter((x) => x !== v)
        : [...ans.filter((x) => x !== "Предложение команды"), v];
      persist();
      b.setAttribute("aria-pressed", p.answers[questionIndex].includes(v));
      $("#next-question").disabled = !answerReady();
      if (!p.answers[questionIndex].includes("Предложение команды"))
        $("#answer-suggestion").innerHTML = "";
      return;
    }
    if (a === "suggest-answer") {
      p.answers[questionIndex] = ["Предложение команды"];
      persist();
      render(false);
      return;
    }
    if (a === "next-question") {
      if (!answerReady()) return;
      questionIndex++;
      if (questionIndex === 3) {
        questionIndex = 2;
        go("team-plan");
      } else go("question");
      return;
    }
    if (a === "discuss-team") {
      return openDialog("Обсудить команду с фасилитатором", `<p>Какой стороны задачи не хватает или какая роль кажется лишней?</p><form id="team-feedback-form"><label class="field"><span>Ваше уточнение</span><textarea name="teamFeedback" required maxlength="2000">${esc(p.teamFeedback || "")}</textarea></label><p class="helper">В демо замечание сохранится в контексте. Произвольный пересмотр ролей потребует подключения CTF; состав не изменится автоматически.</p><button type="submit" class="button primary">Сохранить уточнение</button></form>`);
    }
    if (a === "start-run") return startRun();
    if (a === "decision") {
      p.answer = decisionOptions(p).filter((_, i) => i === Number(b.dataset.index));
      persist();
      document
        .querySelectorAll("[data-action=decision]")
        .forEach((x) => x.setAttribute("aria-pressed", x === b));
      $("#resume").disabled = false;
      return;
    }
    if (a === "resume") {
      if (!p.answer.length && !p.answerCustom.trim()) return;
      p.awaitingAnswered = true;
      p.status = "working";
      event(p, "Автор уточнил направление работы. Решение передано команде.");
      persist();
      render(false);
      return;
    }
    if (a === "approve") return approve();
    if (a === "show-revision") return revisionForm();
    if (a === "cancel-revision") {
      $("#revision-slot").innerHTML = "";
      return;
    }
    if (a === "stage") {
      const n = Number(b.dataset.stage);
      if (n > p.stage) return;
      viewedStage = currentArtifact(p, n) ? n : null;
      render();
      return;
    }
    if (a === "open-artifact") {
      viewedStage = Number(b.dataset.stage);
      render();
      return;
    }
    if (a === "show-results") {
      viewedStage = null;
      render();
      return;
    }
    if (a === "version") {
      const doc = p.artifacts[Number(b.dataset.stage)].find(
        (d) => d.version === Number(b.dataset.version),
      );
      if (doc) openDialog("Сохранённая версия", renderDocument(doc));
      return;
    }
    if (a === "download-one") {
      const doc = currentArtifact(p, Number(b.dataset.stage));
      if (doc) download(p, [doc]);
      return;
    }
    if (a === "download-all")
      return download(p, p.artifacts.map((a) => a.at(-1)).filter(Boolean));
    if (a === "feedback") {
      p.feedback = b.dataset.value;
      persist();
      document
        .querySelectorAll("[data-action=feedback]")
        .forEach((x) => x.setAttribute("aria-pressed", x === b));
      $("#feedback-state").textContent = "Спасибо, оценка сохранена.";
      return;
    }
    if (a === "finish") {
      p.status = "done";
      event(p, "Проект завершён автором. Результаты сохранены.");
      persist();
      go("profile");
      return;
    }
    if (a === "reopen") {
      p.status = "review";
      viewedStage = p.stage;
      persist();
      render();
      revisionForm();
      return;
    }
    if (a === "next-step") {
      p.messages.push({
        role: "user",
        text: `Следующий шаг: ${b.dataset.value}`,
      });
      persist();
      toast("Пожелание сохранено в проекте. Ничего не опубликовано.");
      return;
    }
    if (a === "open-project") {
      store.active = b.dataset.id;
      viewedStage = null;
      persist();
      const current = project();
      questionIndex = current.questionIndex || 0;
      go(current.status === "draft" ? current.setup || "context" : "workspace");
      return;
    }
    if (a === "dismiss-tour") {
      store.tourSeen = true;
      persist();
      b.closest(".workspace-tour").remove();
      return;
    }
    if (a === "materials") return materials();
    if (a === "remove-file") {
      p.files.splice(Number(b.dataset.index), 1);
      p.contextChanged = true;
      persist();
      document
        .querySelectorAll("[data-file-list]")
        .forEach((x) => (x.innerHTML = filesList(p)));
      return;
    }
    if (a === "team")
      return openDialog("Команда и ответственность", teamCards(p) + statusPanel(p));
    if (a === "video") {
      const time = Number(b.dataset.time || 0);
      openDialog(
        "Как CoreTeams собирает команду",
        `<video controls playsinline preload="metadata" poster="${media("team")}" src="${media("video")}" aria-label="Ролик CoreTeams"></video><div class="chapter-list"><button data-action="chapter" data-time="0">00:00 · Ваша задача</button><button data-action="chapter" data-time="36">00:36 · Помощник</button><button data-action="chapter" data-time="42">00:42 · Работа команды</button><button data-action="chapter" data-time="52">00:52 · Единый результат</button></div><p class="helper">Помощник организует работу, специалисты вносят свой вклад, вы принимаете результат.</p>`,
      );
      const v = $("video", $("#dialog"));
      v.dataset.requestedTime = String(time);
      v.addEventListener(
        "loadedmetadata",
        () => {
          v.currentTime = Number(v.dataset.requestedTime || 0);
        },
        { once: true },
      );
      return;
    }
    if (a === "chapter") {
      const v = $("video", $("#dialog"));
      v.dataset.requestedTime = b.dataset.time;
      if (v.readyState >= 1) v.currentTime = Number(b.dataset.time);
      v.play().catch(() => {});
      return;
    }
    if (a === "close-dialog") return closeDialog();
    if (a === "about-demo")
      return openDialog(
        "Об этом прототипе",
        `<p>Это интерактивная версия 9. Она показывает общий путь: задача → контекст → уточнения → предложение команды → план → работа → согласование → результат. Курс — один из сценариев.</p><div class="notice">AI-команда, регистрация, списания и время работы имитируются. Ответы подготовлены заранее и частично используют введённый вами контекст.</div><p class="spacer-top">Файлы не отправляются: сохраняются только названия. Проекты и отзывы остаются в браузере. Реальной авторизации, почты и фоновой обработки после закрытия страницы нет.</p><p class="spacer-top">Операторская и серверные интеграции сохранены в предыдущей версии прототипа; эта версия пересматривает пользовательский путь.</p>`,
      );
    if (a === "book-case")
      return openDialog(
        "«Город между строк»",
        `<img src="${media("book")}" alt="Разворот книги Город между строк" style="border-radius:10px;margin-bottom:20px"><p>Вымышленная книга о городе, хранящем воспоминания жителей. Команда помогает связать структуру, редактуру и оформление.</p><ul><li>Редактор: структура глав и замечания к рукописи.</li><li>Дизайнер: образец разворота и типографика.</li><li>Верстальщик: подготовка макета для InDesign.</li></ul><p class="helper">Это демонстрация формата результата. Готового печатного IDML в этой версии нет.</p>`,
      );
    if (a === "coffee-case")
      return openDialog(
        "Команда для запуска кофейни",
        `<p>Одна идея требует нескольких точек зрения. CoreTeams организует их вокруг вашего решения.</p><ol class="plan-timeline"><li><span class="num">1</span><div><h4>Исследователь</h4><p>Спрос, аудитория и конкуренты.</p></div></li><li><span class="num">2</span><div><h4>Финансист и операционный специалист</h4><p>Расходы, нагрузка и условия окупаемости.</p></div></li><li><span class="num">3</span><div><h4>Маркетолог</h4><p>Позиционирование и первые каналы привлечения.</p></div></li></ol><p>Итог — единый план проверки идеи с допущениями и открытыми вопросами.</p><p class="helper">Для своей бизнес-задачи выберите направление «Бизнес и новая идея» при создании проекта. Демо покажет шаблон команды и плана, а не исследование реального рынка.</p>`,
      );
  });
  document.addEventListener("input", (e) => {
    const p = project();
    if (!p) return;
    const t = e.target;
    if (t.dataset.projectField) p[t.dataset.projectField] = t.value;
    if (t.id === "custom-answer") {
      p.custom[questionIndex] = t.value;
      $("#next-question").disabled = !answerReady();
    }
    if (t.id === "decision-custom") {
      p.answerCustom = t.value;
      $("#resume").disabled = !p.answer.length && !t.value.trim();
    }
    if (t.id === "feedback-note") p.feedbackNote = t.value;
    if (t.id === "message") p.messageDraft = t.value;
    if (t.name === "revision") p.revisionDraft = t.value;
    persist();
  });
  document.addEventListener("change", (e) => {
    if (e.target.id === "scenario-choice") {
      const p = project(), next = e.target.value;
      if (!scenarios[next] || next === scenarioOf(p)) return;
      p.scenario = next;
      p.answers = [[], [], []];
      p.custom = ["", "", ""];
      p.answer = [];
      p.answerCustom = "";
      questionIndex = 0;
      persist();
      toast("Направление обновлено. Уточняющие вопросы и команда будут другими.");
    }
    if (e.target.id === "plan-confirm")
      $("#start-run").disabled = !e.target.checked;
    if (e.target.matches("[data-files]")) {
      const p = project();
      for (const f of e.target.files) {
        if (p.files.length >= 30) {
          toast("В демо можно добавить до 30 файлов в проект.");
          break;
        }
        if (!p.files.some((x) => x.name === f.name))
          p.files.push({ name: f.name, size: f.size });
      }
      p.contextChanged = true;
      persist();
      document
        .querySelectorAll("[data-file-list]")
        .forEach((x) => (x.innerHTML = filesList(p)));
      e.target.value = "";
    }
  });
  document.addEventListener("submit", (e) => {
    const id = e.target.id;
    if (
      ![
        "account-form",
        "team-feedback-form",
        "context-form",
        "materials-form",
        "revision-form",
        "message-form",
      ].includes(id)
    )
      return;
    e.preventDefault();
    const p = project(),
      data = new FormData(e.target);
    if (id === "team-feedback-form") {
      const feedback = String(data.get("teamFeedback") || "").trim();
      if (!feedback) return;
      p.teamFeedback = feedback;
      p.messages.push({ role: "user", text: "Уточнение к команде: " + feedback });
      persist();
      closeDialog();
      toast("Уточнение сохранено. Состав в демо автоматически не меняется.");
      return;
    }
    if (id === "account-form") {
      e.target.reset();
      return go("welcome");
    }
    if (id === "context-form") {
      p.name = String(data.get("name")).trim();
      p.problem = String(data.get("problem")).trim();
      if (!p.name || !p.problem) {
        toast("Добавьте название и хотя бы короткое описание задачи.");
        return;
      }
      p.context = String(data.get("context") || "").trim();
      questionIndex = 0;
      persist();
      return go("question");
    }
    if (id === "materials-form") {
      p.context = String(data.get("context") || "").trim();
      p.contextChanged = true;
      persist();
      closeDialog();
      render(false);
      toast("Контекст сохранён для следующего результата.");
      return;
    }
    if (id === "revision-form") {
      const request = String(data.get("revision")).trim();
      if (!request) return;
      if (p.limit - p.spent < 20) return;
      p.revisionRequest = request;
      p.revisionDraft = "";
      p.revising = true;
      p.status = "working";
      p.progress = 0;
      p.runBase = p.spent;
      p.runCost = 20;
      viewedStage = null;
      event(p, "Автор запросил новую версию результата.");
      persist();
      render();
      startTimer();
      return;
    }
    if (id === "message-form") {
      const text = String(data.get("message")).trim();
      if (!text) return;
      p.messages.push(
        { role: "user", text },
        {
          role: "assistant",
          text: "Уточнение сохранено в памяти проекта. Оно попадёт в следующий результат или доработку. Уже одобренные версии остаются прежними.",
        },
      );
      p.messageDraft = "";
      p.contextChanged = true;
      persist();
      render(false);
      toast("Уточнение добавлено к проекту.");
    }
  });
  $("#dialog").addEventListener("cancel", (e) => {
    e.preventDefault();
    closeDialog();
  });
  $("#dialog").addEventListener("click", (e) => {
    if (e.target === $("#dialog")) {
      const r = e.target.getBoundingClientRect();
      if (
        e.clientX < r.left ||
        e.clientX > r.right ||
        e.clientY < r.top ||
        e.clientY > r.bottom
      )
        closeDialog();
    }
  });
  window.addEventListener("hashchange", () => {
    readRoute();
    render();
  });
  readRoute();
  render(false);
  startTimer();
})();
