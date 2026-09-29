"use strict";

(() => {
try {

const $ = (selector) => document.querySelector(selector);
const pages = [...document.querySelectorAll(".page")];
const state = {
  page: "home",
  level: "",
  question: 0,
  answers: [],
  customAnswers: [],
  materials: JSON.parse(localStorage.getItem("ct-materials") || "[]"),
  context: localStorage.getItem("ct-context") || "",
  projectType: localStorage.getItem("ct-project-type") || "business",
  projectName: localStorage.getItem("ct-project-name") || "Кофейня у университета",
  projectGoal: localStorage.getItem("ct-project-goal") || "Хочу открыть небольшую кофейню рядом с университетом и понять, насколько идея жизнеспособна и что потребуется для запуска.",
  projects: JSON.parse(localStorage.getItem("ct-projects") || "[]"),
  currentProjectId: localStorage.getItem("ct-current-project") || "",
  revision: false,
  credits: Number(localStorage.getItem("ct-credits") || 0)
};
const PROJECT_LIMIT = 3;

const genericQuestions = [
  {
    title: "Что лучше описывает ваш запрос?",
    help: "Ответ поможет выбрать формат первого результата.",
    options: ["Есть идея, которую хочу развить", "Есть проблема, но решения пока нет", "Пока не знаю — предложите варианты"]
  },
  {
    title: "Для кого создаём первую версию?",
    help: "От аудитории зависят сценарий, тексты и состав команды.",
    options: ["Для клиентов", "Для своей команды", "Для студентов или учеников", "Пока не знаю — предложите вариант"]
  },
  {
    title: "Что должно измениться после результата?",
    help: "Так команда поймёт, по какому признаку проверять работу.",
    options: ["Люди смогут выполнить задачу", "Появится материал для обсуждения", "Можно будет проверить спрос", "Пока не знаю — сделайте предположение"]
  }
];

const businessQuestions = [
  {
    title: "На каком этапе находится идея?",
    help: "Так команда поймёт, что нужно проверить сначала: саму возможность запуска или уже конкретную модель.",
    options: ["Пока только идея", "Уже ищу помещение", "Есть предварительные расчёты", "Бизнес уже работает"]
  },
  {
    title: "Что вызывает больше всего сомнений?",
    help: "Ответ определит первый участок работы и последовательность передачи выводов между специалистами.",
    options: ["Будут ли клиенты", "Хватит ли бюджета", "Как выбрать место", "Как организовать работу", "Пока не знаю — найдите слабые места"]
  },
  {
    title: "Что уже определено?",
    help: "Команда не будет заново решать то, что вы уже зафиксировали.",
    options: ["Город или район", "Формат и ассортимент", "Примерный бюджет", "Срок запуска", "Пока ничего"]
  },
  {
    title: "Какие ограничения нельзя нарушать?",
    help: "Ограничения станут общими рамками для финансовой, операционной и маркетинговой частей.",
    options: ["Ограниченный бюджет", "Жёсткий срок", "Небольшая площадь", "Минимальная команда", "Пока не знаю — предложите рамки"]
  },
  {
    title: "Какой результат поможет принять решение?",
    help: "По этому критерию независимый проверяющий оценит общий результат команды.",
    options: ["Понять, стоит ли запускаться", "Получить предварительную экономику", "Получить пошаговый план запуска", "Сравнить несколько сценариев"]
  }
];

const curriculumQuestions = [
  {
    title: "Для кого предназначена программа?",
    help: "Уровень слушателей определяет сложность, темп и допустимую нагрузку.",
    options: ["Студенты бакалавриата", "Взрослые на переподготовке", "Сотрудники компании", "Пока не знаю — предложите аудиторию"]
  },
  {
    title: "Какой результат пересмотра вам нужен?",
    help: "Можно найти проблемы, обновить содержание или полностью пересобрать программу.",
    options: ["Найти проблемы и риски", "Обновить устаревшие темы", "Связать цели, задания и оценивание", "Подготовить полностью обновлённую версию"]
  },
  {
    title: "Что в программе нельзя менять?",
    help: "Команда не будет предлагать решения, нарушающие эти границы.",
    options: ["Продолжительность и количество часов", "Обязательные темы", "Формат итоговой аттестации", "Жёстких ограничений нет"]
  },
  {
    title: "Какие ограничения нужно учитывать?",
    help: "Например, образовательный стандарт, сроки, преподаватели или доступная платформа.",
    options: ["Есть ограничение по срокам или часам", "Нужно соблюдать стандарт", "Нужно учитывать доступные ресурсы", "Пока не знаю — найдите ограничения в материалах"]
  },
  {
    title: "По какому признаку результат будет хорошим?",
    help: "Этот критерий станет рубрикой независимой проверки.",
    options: ["Цели связаны с заданиями и оценкой", "Нагрузка реалистична", "Содержание актуально", "Результаты обучения сформулированы однозначно"]
  }
];

const bookQuestions = [
  { title: "На какой стадии книга?", help: "От этого зависит состав команды.", options: ["Есть идея и синопсис", "Есть черновик", "Рукопись закончена", "Нужна помощь определить формат"] },
  { title: "Для кого эта книга?", help: "Аудитория влияет на редактуру, объём и оформление.", options: ["Взрослая художественная проза", "Подростковая аудитория", "Детская книга", "Нон-фикшн"] },
  { title: "Какой результат нужен первым?", help: "Команда ограничит первую версию одним проверяемым артефактом.", options: ["Редакторский разбор", "Структура и план глав", "Демонстрационный разворот", "Макет для печати"] }
];

function activeQuestions() {
  if (state.projectType === "business") return businessQuestions;
  if (state.projectType === "curriculum") return curriculumQuestions;
  if (state.projectType === "book") return bookQuestions;
  return genericQuestions;
}

const teams = {
  business: [
    ["Исследователь рынка", "Проверяет аудиторию, спрос, место и конкурентов"],
    ["Финансовый аналитик", "Считает вложения, расходы, выручку и точку безубыточности"],
    ["Операционный специалист", "Определяет процессы, оборудование, поставщиков и загрузку команды"],
    ["Маркетолог", "Собирает способ привлечения и удержания первых клиентов"],
    ["Специалист по требованиям", "Проверяет обязательные разрешения и ограничения запуска"],
    ["Риск-аналитик", "Сверяет модель, допущения и критические риски перед итогом"]
  ],
  curriculum: [
    ["Методист", "Проверяет цели, темы, задания и оценивание"],
    ["Предметный эксперт", "Ищет устаревшие и спорные положения"],
    ["Аналитик нагрузки", "Проверяет объём работы и реалистичность сроков"],
    ["Редактор", "Собирает ясную обновлённую программу"],
    ["Независимый проверяющий", "Сверяет результат с согласованной рубрикой"]
  ],
  book: [
    ["Литературный редактор", "Проверяет структуру и развитие истории"],
    ["Корректор", "Убирает языковые и типографические ошибки"],
    ["Арт-директор", "Создаёт визуальную систему книги"],
    ["Верстальщик", "Готовит разворот и структуру печатного макета"],
    ["Предпечатный проверяющий", "Проверяет комплектность файлов"]
  ],
  custom: [
    ["Аналитик", "Уточняет потребность и критерий результата"],
    ["Дизайнер", "Собирает понятный пользовательский путь"],
    ["Архитектор", "Определяет границы первой версии"],
    ["Разработчик", "Создаёт согласованный результат"],
    ["Тестировщик", "Независимо проверяет работу"]
  ]
};

function escapeHtml(value) {
  return String(value).replace(/[&<>"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char]);
}

function answerValues(index) {
  const selected = Array.isArray(state.answers[index]) ? state.answers[index] : state.answers[index] ? [state.answers[index]] : [];
  const custom = (state.customAnswers[index] || "").trim();
  return custom ? [...selected, custom] : selected;
}

function answerText(index, fallback = "Не указано") {
  const values = answerValues(index);
  return values.length ? values.join("; ") : fallback;
}

function persistProjects() {
  localStorage.setItem("ct-projects", JSON.stringify(state.projects));
  localStorage.setItem("ct-current-project", state.currentProjectId);
}

function upsertProject(status) {
  if (!state.currentProjectId) state.currentProjectId = `project-${Date.now()}`;
  const record = {
    id: state.currentProjectId,
    name: state.projectName,
    goal: state.projectGoal,
    type: state.projectType,
    status,
    updatedAt: new Date().toISOString()
  };
  const index = state.projects.findIndex((project) => project.id === record.id);
  if (index >= 0) state.projects[index] = record;
  else state.projects.unshift(record);
  persistProjects();
  renderProfile();
}

function renderProfile() {
  const used = state.projects.length;
  const remaining = Math.max(0, PROJECT_LIMIT - used);
  $("#project-limit-count").textContent = `${used} из ${PROJECT_LIMIT}`;
  $("#project-limit-bar").style.width = `${Math.min(100, (used / PROJECT_LIMIT) * 100)}%`;
  $("#project-limit-note").textContent = remaining ? `Можно создать ещё ${remaining}` : "Лимит достигнут";
  $("#new-project").disabled = remaining === 0;
  $("#project-limit-error").textContent = remaining ? "" : "Чтобы создать новый проект, завершите или удалите один из существующих.";
  $("#profile-projects").innerHTML = used
    ? state.projects.map((project) => `<article class="profile-project"><div><span class="eyebrow">${escapeHtml(project.type === "business" ? "Бизнес-задача" : project.type === "curriculum" ? "Учебная программа" : project.type === "book" ? "Книга" : "Продукт или сервис")}</span><h3>${escapeHtml(project.name)}</h3><p>${escapeHtml(project.goal)}</p></div><div><span class="profile-status">${escapeHtml(project.status)}</span><button class="ghost" data-open-project="${escapeHtml(project.id)}">Открыть</button></div></article>`).join("")
    : '<div class="profile-empty">Проектов пока нет. Создайте первый — его прогресс и результат появятся здесь.</div>';
  document.querySelectorAll("[data-open-project]").forEach((button) => button.addEventListener("click", () => {
    const project = state.projects.find((item) => item.id === button.dataset.openProject);
    if (!project) return;
    state.currentProjectId = project.id;
    state.projectName = project.name;
    state.projectGoal = project.goal;
    state.projectType = project.type;
    persistProjects();
    $("#project-name").value = state.projectName;
    $("#project-goal").value = state.projectGoal;
    showPage(["Завершён", "Результат готов"].includes(project.status) ? "result" : "project-setup");
  }));
}

function showPage(id, remember = true) {
  pages.forEach((page) => page.classList.add("hidden"));
  const page = document.getElementById(id);
  if (!page) return;
  page.classList.remove("hidden");
  state.page = id;
  if (id === "profile") renderProfile();
  if (id === "result") {
    $("#result-title").textContent = state.projectName;
    $("#result-copy").textContent = "Core Team Framework объединил выводы специалистов, согласованные решения и независимую проверку в один общий результат.";
    $("#save-result-state").textContent = state.projects.find((project) => project.id === state.currentProjectId)?.status === "Завершён"
      ? "Результат сохранён в профиле."
      : "Результат ещё не сохранён в профиле.";
  }
  if (remember && !["signup", "development"].includes(id)) localStorage.setItem("ct-page", id);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderMaterials() {
  $("#material-count").textContent = state.materials.length;
  $("#material-list").innerHTML = state.materials.length
    ? state.materials.map((name) => `<li>${name}</li>`).join("")
    : "<li>Материалов пока нет.</li>";
  $("#summary-files").textContent = state.materials.length ? state.materials.join(", ") : "Не добавлены";
  $("#summary-context").textContent = state.context || "Не добавлен";
  $("#global-context").value = state.context;
  $("#context-state").textContent = state.context ? "Контекст сохранён и будет учтён командой автоматически." : "Контекст пока не добавлен.";
  $("#workspace-files").textContent = state.materials.length;
  $("#workspace-context-status").textContent = state.context ? "Добавлен" : "Не добавлен";
  $("#workspace-context-text").textContent = state.context || "Контекст пока не добавлен. Его можно добавить в любой момент.";
}

function openDrawer(id) {
  document.querySelectorAll(".drawer").forEach((drawer) => drawer.classList.add("hidden"));
  $(id).classList.remove("hidden");
}

function renderQuestion() {
  const current = activeQuestions()[state.question];
  const selected = Array.isArray(state.answers[state.question]) ? state.answers[state.question] : [];
  $("#question-title").textContent = current.title;
  $("#question-help").textContent = current.help;
  $("#answer-options").innerHTML = current.options.map((option) => `<button class="${selected.includes(option) ? "selected" : ""}" aria-pressed="${selected.includes(option)}" data-answer="${escapeHtml(option)}">${escapeHtml(option)}</button>`).join("");
  $("#answer-custom").value = state.customAnswers[state.question] || "";
  const updateNext = () => { $("#question-next").disabled = answerValues(state.question).length === 0; };
  updateNext();
  document.querySelectorAll("[data-answer]").forEach((button) => {
    button.addEventListener("click", () => {
      const values = Array.isArray(state.answers[state.question]) ? [...state.answers[state.question]] : [];
      const index = values.indexOf(button.dataset.answer);
      if (index >= 0) values.splice(index, 1); else values.push(button.dataset.answer);
      state.answers[state.question] = values;
      button.classList.toggle("selected", index < 0);
      button.setAttribute("aria-pressed", String(index < 0));
      updateNext();
    });
  });
  $("#answer-custom").oninput = (event) => {
    state.customAnswers[state.question] = event.target.value;
    updateNext();
  };
}

function fillSummary() {
  $("#summary-result").textContent = state.projectGoal;
  $("#summary-audience").textContent = answerText(0, "Команда предложит аудиторию");
  $("#summary-outcome").textContent = state.projectType === "curriculum"
    ? `${answerText(1, "Формат пересмотра уточняется")}; проверка: ${answerText(activeQuestions().length - 1, "критерий уточняется")}`
    : state.projectType === "business"
      ? answerText(activeQuestions().length - 1, "Критерий уточнит фасилитатор")
      : answerText(1, "Критерий уточнит фасилитатор");
  $("#summary-directions").textContent = state.projectType === "business"
    ? "Спрос, экономика, операции, привлечение клиентов, требования и риски"
    : state.projectType === "book"
      ? "Структура, редактура, визуальная система, макет и комплектность"
      : state.projectType === "curriculum"
        ? "Цели, содержание, нагрузка, оценивание и актуальность"
        : "Потребность, пользовательский путь, границы решения и проверка качества";
  const unknown = state.answers.flatMap((answer) => Array.isArray(answer) ? answer : [answer]).filter((answer) => /не знаю|предполож/i.test(answer || "")).length;
  $("#summary-assumption").textContent = unknown
    ? unknown === 1
      ? "Команда предложит 1 безопасное предположение для подтверждения"
      : `Команда предложит ${unknown} безопасных предположения для подтверждения`
    : "Критичных предположений нет";
  renderMaterials();
}

function renderTeamPlan() {
  const selectedTeam = teams[state.projectType] || teams.custom;
  $("#team-plan-list").innerHTML = selectedTeam.map(([role, responsibility], index) => `
    <article class="team-plan-card">
      <span>${String(index + 1).padStart(2, "0")}</span>
      <div><h3>${escapeHtml(role)}</h3><p>${escapeHtml(responsibility)}</p></div>
      <b>${index === 0 ? "Начинает" : index === selectedTeam.length - 1 ? "Проверяет итог" : "Получает выводы выше"}</b>
    </article>`).join("");
  $("#team-why-copy").textContent = state.projectType === "business"
    ? "Для проверки бизнес-идеи недостаточно одной точки зрения: данные о спросе влияют на финансовую модель, экономика — на операционный формат, а ограничения и риски проверяются до итоговой рекомендации."
    : "Состав отражает разные части задачи. Роли работают не параллельными чатами: их ответственность разделена, а выводы передаются по общей последовательности.";
}

function fillLanding() {
  const audience = answerText(0, "Аудитория уточняется");
  const outcome = state.projectType === "business"
    ? answerText(activeQuestions().length - 1, "Проверить жизнеспособность идеи")
    : answerText(1, "Проверить ценность решения");
  const proposedTitle = state.projectType === "curriculum"
    ? "Курс, в котором цели ведут к результату"
    : state.projectType === "book"
      ? "Книга, готовая к редактуре и выпуску"
      : state.projectType === "business"
        ? "Кофейня, решение о которой основано на расчётах"
      : state.projectName;
  $("#landing-title").textContent = proposedTitle;
  $("#landing-copy").textContent = state.projectType === "business"
    ? `${state.projectGoal} Исходная ситуация: ${audience.toLowerCase()}. Критерий результата: ${outcome.toLowerCase()}.`
    : `${state.projectGoal} Аудитория: ${audience}. Первый фокус: ${outcome.toLowerCase()}.`;
  $("#landing-audience").textContent = state.projectType === "business" ? `Этап: ${audience}` : audience;
}

function updateCredits(value) {
  state.credits = value;
  localStorage.setItem("ct-credits", String(value));
  $("#credits-used").textContent = (1000 - value).toLocaleString("ru-RU");
}

function setRunStatus(status) {
  const labels = { queued: "В очереди", running: "В работе", waiting_for_input: "Нужен ответ", completed: "Завершено" };
  $("#run-status").dataset.status = status;
  $("#run-status").textContent = labels[status] || status;
}

function showRunProblem(status) {
  const copy = {
    failed: ["Ошибка", "Работу не удалось завершить", "Данные проекта и последняя подтверждённая версия сохранены.", "Можно повторить запуск или вернуться в профиль."],
    unknown: ["Результат не подтверждён", "Связь с исполнителем прервалась", "Мы не помечаем работу завершённой, пока сохранение результата не подтверждено.", "Последняя подтверждённая версия доступна. Запуск можно повторить."],
    limit_reached: ["Лимит достигнут", "Запуск не начался", "Доступного лимита недостаточно. Команда не запускалась и кредиты не списывались.", "Вернитесь в профиль или повторите после восстановления лимита."]
  }[status];
  $("#problem-status").dataset.status = status;
  $("#problem-status").textContent = copy[0];
  $("#problem-title").textContent = copy[1];
  $("#problem-copy").textContent = copy[2];
  $("#problem-available").textContent = copy[3];
  localStorage.setItem("ct-run", JSON.stringify({ status, projectId: state.currentProjectId }));
  showPage("run-problem");
}

function runDevelopment(isRevision = false) {
  state.revision = isRevision;
  showPage("development", false);
  $("#credit-badge").classList.remove("hidden");
  $("#agent-list").innerHTML = "";
  $("#progress-bar").style.width = "0%";
  $("#progress-percent").textContent = "0%";
  $("#development-question").classList.add("hidden");
  $("#development-answer-custom").value = "";
  setRunStatus("queued");
  $("#development-title").textContent = isRevision ? "Команда дорабатывает результат" : "Команда работает над общей задачей";
  $("#workspace-project").textContent = state.projectName;
  $("#workspace-files").textContent = state.materials.length;
  $("#workspace-context-status").textContent = state.context ? "Добавлен" : "Не добавлен";
  $("#workspace-context-text").textContent = state.context || "Контекст пока не добавлен. Его можно добавить в любой момент.";
  $("#activity-feed").innerHTML = `<p>Проект «${state.projectName}» сохранён.</p><p>${state.materials.length ? `Команда получила материалов: ${state.materials.length}.` : "Материалы не приложены — фасилитатор учтёт это в вопросах."}</p><p>${state.context ? "Контекст автоматически передан всем подключённым ролям." : "Контекст можно добавить во время работы."}</p>`;
  localStorage.setItem("ct-run", JSON.stringify({ status: "running", revision: isRevision, startedAt: Date.now() }));

  let index = 0;
  const selectedTeam = isRevision
    ? [["Фасилитатор", "Уточняет запрос на изменение"], ["Дизайнер", "Обновляет структуру результата"], ["Разработчик", "Вносит согласованные изменения"], ["Тестировщик", "Сравнивает новую версию с запросом"]]
    : teams[state.projectType] || teams.custom;

  const initialMinutes = isRevision ? 4 : ({ business: 7, curriculum: 5, book: 7, custom: 6 }[state.projectType] || 6);
  const questionIndex = Math.min(2, Math.max(1, selectedTeam.length - 1));
  let questionResolved = false;
  const questions = {
    business: {
      title: "Какой сценарий запуска рассчитать первым?",
      help: "Исследователь рынка закончил первичную проверку. Выбор формата повлияет на аренду, оборудование, штат и финансовую модель.",
      options: ["Небольшая точка навынос", "Кофейня с посадочными местами", "Сравнить оба сценария"]
    },
    curriculum: {
      title: "Что важнее, если программа не помещается в заданное число часов?",
      help: "Аналитик обнаружил конфликт между объёмом тем и доступной нагрузкой. Выберите приоритет — команда учтёт его в результате.",
      options: ["Сохранить количество часов и сократить темы", "Сохранить все темы и уменьшить глубину", "Подготовить оба варианта для сравнения"]
    },
    book: {
      title: "Что важнее при редактуре спорного фрагмента?",
      help: "Редактору нужен ваш приоритет, прежде чем команда продолжит работу над макетом.",
      options: ["Сохранить авторский голос", "Упростить текст для читателя", "Подготовить два варианта для сравнения"]
    },
    custom: {
      title: "Какой приоритет важнее для первой версии?",
      help: "Фасилитатор сверяет направление работы, чтобы роли не принимали важное решение за вас.",
      options: ["Быстрее получить рабочую версию", "Сделать результат максимально полным", "Подготовить варианты для сравнения"]
    }
  };

  const updateEta = () => {
    const remainingShare = Math.max(0, (selectedTeam.length - index) / selectedTeam.length);
    const minutes = Math.max(1, Math.ceil(initialMinutes * remainingShare));
    $("#run-eta").textContent = index >= selectedTeam.length ? "Меньше минуты" : `Осталось примерно ${minutes} мин.`;
  };

  const askDevelopmentQuestion = () => {
    const question = questions[state.projectType] || questions.custom;
    const panel = $("#development-question");
    const options = $("#development-question-options");
    const custom = $("#development-answer-custom");
    const submit = $("#development-answer-submit");
    let selected = "";

    setRunStatus("waiting_for_input");
    $("#run-eta").textContent = "Ожидаем ваш ответ · время приостановлено";
    $("#facilitator-live-text").textContent = "Команде нужно ваше решение. Я сформулировал один уточняющий вопрос.";
    $("#development-question-title").textContent = question.title;
    $("#development-question-help").textContent = question.help;
    options.innerHTML = "";
    custom.value = "";
    submit.disabled = true;

    question.options.forEach((option) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = option;
      button.onclick = () => {
        selected = option;
        options.querySelectorAll("button").forEach((item) => item.classList.toggle("selected", item === button));
        submit.disabled = false;
      };
      options.append(button);
    });
    custom.oninput = () => { submit.disabled = !custom.value.trim() && !selected; };
    submit.onclick = () => {
      const answer = custom.value.trim() || selected;
      if (!answer) return;
      questionResolved = true;
      panel.classList.add("hidden");
      const event = document.createElement("p");
      event.textContent = `Фасилитатор получил уточнение: ${answer}`;
      $("#activity-feed").append(event);
      $("#facilitator-live-text").textContent = "Ответ передан всем нужным ролям. Команда продолжает работу.";
      setRunStatus("running");
      localStorage.setItem("ct-run", JSON.stringify({ status: "running", revision: isRevision, resumedAt: Date.now() }));
      updateEta();
      setTimeout(tick, 350);
    };
    panel.classList.remove("hidden");
    localStorage.setItem("ct-run", JSON.stringify({ status: "waiting_for_input", revision: isRevision, question: question.title }));
  };

  $("#run-eta").textContent = `Осталось примерно ${initialMinutes} мин.`;

  const tick = () => {
    if (!questionResolved && index === questionIndex) {
      askDevelopmentQuestion();
      return;
    }
    setRunStatus("running");
    const percent = Math.round((index / selectedTeam.length) * 90);
    $("#progress-bar").style.width = `${percent}%`;
    $("#progress-percent").textContent = `${percent}%`;
    updateCredits(Math.min(300, state.credits + 48));

    if (index < selectedTeam.length) {
      const [role, work] = selectedTeam[index];
      $("#facilitator-live-text").textContent = `Подключаю роль «${role}»: ${work.toLowerCase()}.`;
      const card = document.createElement("article");
      card.className = "agent-card";
      card.innerHTML = `<b>${role}</b><p>${work}</p><span>Работает</span>`;
      $("#agent-list").append(card);
      const event = document.createElement("p");
      event.textContent = `${role}: ${work}`;
      $("#activity-feed").append(event);
      index += 1;
      updateEta();
      setTimeout(tick, 720);
      return;
    }

    $("#progress-bar").style.width = "100%";
    $("#progress-percent").textContent = "100%";
    setRunStatus("completed");
    $("#run-eta").textContent = "Готово";
    $("#facilitator-live-text").textContent = "Команда завершила работу. Общий результат сохранён.";
    updateCredits(isRevision ? Math.min(1000, state.credits) : 240);
    localStorage.setItem("ct-run", JSON.stringify({ status: "completed", revision: isRevision }));
    upsertProject("Результат готов");
    setTimeout(() => {
      $("#result-title").textContent = isRevision ? "Обновлённая версия результата" : "Первая версия результата";
      $("#result-copy").textContent = isRevision
        ? "Фасилитатор собрал запрос, нужные роли внесли изменения, тестировщик проверил новую версию. Предыдущая версия сохранена."
        : "Специалисты завершили свои зоны ответственности, передали выводы по общей последовательности, а фасилитатор собрал их в один согласованный результат.";
      showPage("result");
    }, 600);
  };
  setTimeout(tick, 450);
}

document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
$("#materials-toggle").addEventListener("click", () => openDrawer("#materials-panel"));
$("#notifications-toggle").addEventListener("click", () => openDrawer("#notifications-panel"));
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.close).classList.add("hidden")));
function addFiles(files) {
  const names = [...files].map((file) => file.name);
  state.materials = [...new Set([...state.materials, ...names])];
  localStorage.setItem("ct-materials", JSON.stringify(state.materials));
  renderMaterials();
}
$("#global-files").addEventListener("change", (event) => {
  addFiles(event.target.files);
});
$("#project-files").addEventListener("change", (event) => addFiles(event.target.files));
$("#save-context").addEventListener("click", () => {
  state.context = $("#global-context").value.trim();
  localStorage.setItem("ct-context", state.context);
  renderMaterials();
});
$("#play-demo").addEventListener("click", () => showPage("about"));
$("#show-coffee-example").addEventListener("click", () => showPage("catalog"));
$("#begin").addEventListener("click", () => {
  showPage(state.projects.length ? "profile" : "signup", false);
});

function setCoffeeExample() {
  state.projectType = "business";
  state.projectName = "Кофейня у университета";
  state.projectGoal = "Хочу открыть небольшую кофейню рядом с университетом и понять, насколько идея жизнеспособна и что потребуется для запуска.";
  state.context = "";
  state.answers = [];
  state.customAnswers = [];
  $("#project-name").value = state.projectName;
  $("#project-goal").value = state.projectGoal;
  $("#project-context").value = "";
  document.querySelectorAll("#project-type button").forEach((item) => item.classList.toggle("selected", item.dataset.type === "business"));
}

$("#start-coffee-case").addEventListener("click", () => {
  setCoffeeExample();
  showPage(state.projects.length ? "project-setup" : "signup", false);
});

$("#signup-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const password = $("#password").value;
  if (password !== $("#password-repeat").value) {
    $("#signup-error").textContent = "Пароли не совпадают.";
    return;
  }
  $("#signup-error").textContent = "";
  showPage("onboarding", false);
});

$("#onboarding-next").addEventListener("click", () => showPage("project-setup", false));
document.querySelectorAll("#project-type button").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("#project-type button").forEach((item) => item.classList.remove("selected"));
  button.classList.add("selected");
  state.projectType = button.dataset.type;
  if (state.projectType === "business") {
    $("#project-name").value = "Кофейня у университета";
    $("#project-goal").value = "Хочу открыть небольшую кофейню рядом с университетом и понять, насколько идея жизнеспособна и что потребуется для запуска.";
  } else if (state.projectType === "book") {
    $("#project-name").value = "Создание книги";
    $("#project-goal").value = "Подготовить рукопись к редактуре, собрать демонстрационный разворот и план печатного макета.";
  } else if (state.projectType === "curriculum") {
    $("#project-name").value = "Пересмотр учебной программы";
    $("#project-goal").value = "Найти проблемы в программе курса и подготовить обновлённую версию с понятными рекомендациями.";
  } else {
    $("#project-name").value = "Новый продукт или сервис";
    $("#project-goal").value = "Хочу проверить идею, определить первую версию и понять, что потребуется для запуска.";
  }
}));
$("#project-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!state.currentProjectId && state.projects.length >= PROJECT_LIMIT) {
    showPage("profile");
    $("#project-limit-error").textContent = "Достигнут лимит: одновременно можно хранить не больше 3 проектов.";
    return;
  }
  state.projectName = $("#project-name").value.trim();
  state.projectGoal = $("#project-goal").value.trim();
  state.context = $("#project-context").value.trim();
  localStorage.setItem("ct-project-type", state.projectType);
  localStorage.setItem("ct-project-name", state.projectName);
  localStorage.setItem("ct-project-goal", state.projectGoal);
  localStorage.setItem("ct-context", state.context);
  upsertProject("Черновик");
  renderMaterials();
  showPage("level");
});

document.querySelectorAll("#level-options button").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("#level-options button").forEach((item) => item.classList.remove("selected"));
  button.classList.add("selected");
  state.level = button.dataset.value;
  $("#level-next").disabled = false;
}));
$("#level-next").addEventListener("click", () => {
  state.question = 0;
  renderQuestion();
  showPage("facilitator");
});
$("#question-next").addEventListener("click", () => {
  if (state.question < activeQuestions().length - 1) {
    state.question += 1;
    renderQuestion();
  } else {
    fillSummary();
    showPage("summary");
  }
});
$("#summary-edit").addEventListener("click", () => {
  state.question = 0;
  renderQuestion();
  showPage("facilitator");
});
$("#make-team").addEventListener("click", () => {
  renderTeamPlan();
  showPage("team-plan");
});
$("#team-back").addEventListener("click", () => showPage("summary"));
$("#team-approve").addEventListener("click", () => {
  fillLanding();
  showPage("landing");
});
$("#landing-revise").addEventListener("click", () => showPage("summary"));
$("#landing-approve").addEventListener("click", () => showPage("estimate"));
$("#estimate-back").addEventListener("click", () => showPage("landing"));
$("#development-start").addEventListener("click", () => {
  updateCredits(0);
  runDevelopment(false);
});
$("#simulate-failed").addEventListener("click", () => showRunProblem("failed"));
$("#simulate-unknown").addEventListener("click", () => showRunProblem("unknown"));
$("#simulate-limit").addEventListener("click", () => showRunProblem("limit_reached"));
$("#problem-profile").addEventListener("click", () => showPage("profile"));
$("#problem-retry").addEventListener("click", () => showPage("estimate"));
$("#request-revision").addEventListener("click", () => showPage("revision"));
let revisionTypeSelected = false;
function updateRevisionButton() {
  $("#revision-start").disabled = !revisionTypeSelected || !$("#revision-text").value.trim();
}
document.querySelectorAll("#revision-type button").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("#revision-type button").forEach((item) => item.classList.remove("selected"));
  button.classList.add("selected");
  revisionTypeSelected = true;
  updateRevisionButton();
}));
$("#revision-text").addEventListener("input", updateRevisionButton);
$("#revision-start").addEventListener("click", () => runDevelopment(true));
$("#download-artifact").addEventListener("click", () => {
  const answers = activeQuestions().map((question, index) => `### ${question.title}\n\n${answerText(index)}`).join("\n\n");
  const content = `# ${state.projectName}\n\n## Цель\n\n${state.projectGoal}\n\n## Контекст\n\n${state.context || "Контекст не добавлен."}\n\n## Ответы фасилитатору\n\n${answers}\n\n## Состав команды\n\n${(teams[state.projectType] || teams.custom).map(([role, responsibility]) => `- **${role}:** ${responsibility}`).join("\n")}\n\n## Итог\n\nCore Team Framework распределил ответственность между специалистами и объединил их выводы в один общий результат.\n`;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
  link.download = `${state.projectName.replace(/[\\/:*?"<>|]/g, "-") || "артефакт"}.md`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});
$("#finish-project").addEventListener("click", () => {
  upsertProject("Завершён");
  $("#save-result-state").textContent = "Результат сохранён в профиле. Его можно открыть позже без повторной загрузки материалов.";
  showPage("next");
});
$("#open-profile").addEventListener("click", () => showPage("profile"));
$("#new-project").addEventListener("click", () => {
  if (state.projects.length >= PROJECT_LIMIT) {
    $("#project-limit-error").textContent = "Достигнут лимит: одновременно можно хранить не больше 3 проектов.";
    return;
  }
  state.currentProjectId = "";
  state.projectType = "business";
  state.projectName = "Кофейня у университета";
  state.projectGoal = "Хочу открыть небольшую кофейню рядом с университетом и понять, насколько идея жизнеспособна и что потребуется для запуска.";
  state.context = "";
  state.materials = [];
  state.answers = [];
  state.customAnswers = [];
  localStorage.setItem("ct-context", "");
  localStorage.setItem("ct-materials", "[]");
  localStorage.setItem("ct-current-project", "");
  $("#project-name").value = state.projectName;
  $("#project-goal").value = state.projectGoal;
  $("#project-context").value = "";
  document.querySelectorAll("#project-type button").forEach((item) => item.classList.toggle("selected", item.dataset.type === "business"));
  renderMaterials();
  showPage("project-setup");
});
$("#scale").addEventListener("click", () => {
  $("#next-note").textContent = "Фасилитатор подготовит отдельный диалог о целях, ресурсах и границах роста. В демо процесс не запускается.";
});
$("#invest").addEventListener("click", () => {
  $("#next-note").textContent = "Сначала сервис запросит согласие на состав публичных материалов. В демо данные не публикуются и никому не передаются.";
});

function selectAdminTab(tabName) {
  document.querySelectorAll("[data-admin-tab]").forEach((button) => button.classList.toggle("active", button.dataset.adminTab === tabName));
  document.querySelectorAll("[data-admin-panel]").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.adminPanel !== tabName));
}

document.querySelectorAll("[data-admin-tab]").forEach((button) => button.addEventListener("click", () => selectAdminTab(button.dataset.adminTab)));

$("#admin-run-filter").addEventListener("change", (event) => {
  document.querySelectorAll("#admin-runs-body tr").forEach((row) => {
    row.classList.toggle("hidden", event.target.value !== "all" && row.dataset.runState !== event.target.value);
  });
});

$("#admin-user-search").addEventListener("input", (event) => {
  const query = event.target.value.trim().toLowerCase();
  document.querySelectorAll("#admin-users-body tr").forEach((row) => row.classList.toggle("hidden", !row.dataset.userSearch.includes(query)));
});

function openAdminDialog(kind, id) {
  const dialog = $("#admin-dialog");
  if (kind === "run") {
    $("#admin-dialog-eyebrow").textContent = "Безопасная диагностика";
    $("#admin-dialog-title").textContent = `Запуск ${id}`;
    $("#admin-dialog-body").innerHTML = `<dl class="diagnostic-list"><div><dt>Состояние</dt><dd>${id === "run_1048" ? "Нет событий 8 минут" : "Работает штатно"}</dd></div><div><dt>Последняя подтверждённая операция</dt><dd>${id === "run_1048" ? "Риск-аналитик получил входные данные" : "Событие принято и сохранено"}</dd></div><div><dt>Доступно администратору</dt><dd>Время, состояние, роли и корреляционный номер</dd></div><div><dt>Недоступно</dt><dd>Материалы проекта, скрытые рассуждения, промпты и секреты</dd></div></dl>`;
    $("#admin-dialog-action").textContent = "Пометить для разбора";
  } else {
    $("#admin-dialog-eyebrow").textContent = "Карточка пользователя";
    $("#admin-dialog-title").textContent = id;
    $("#admin-dialog-body").innerHTML = `<p class="admin-dialog-note">Администратору доступны статус аккаунта, количество проектов, активные запуски и лимит. Содержимое проектов закрыто.</p><dl class="diagnostic-list"><div><dt>Статус</dt><dd>Активен</dd></div><div><dt>Уровень доступа</dt><dd>Пользователь</dd></div><div><dt>Последний вход</dt><dd>Сегодня, 12:08</dd></div><div><dt>Служебный доступ</dt><dd>Не запрашивался</dd></div></dl>`;
    $("#admin-dialog-action").textContent = "Изменить тестовый лимит";
  }
  $("#admin-dialog-action").onclick = () => {
    $("#admin-dialog-body").insertAdjacentHTML("beforeend", '<p class="context-state">Демо: действие не выполнено. В рабочей версии потребуется причина, подтверждение и запись в аудите.</p>');
    $("#admin-dialog-action").disabled = true;
  };
  $("#admin-dialog-action").disabled = false;
  dialog.showModal();
}

document.querySelectorAll("[data-open-run]").forEach((button) => button.addEventListener("click", () => {
  selectAdminTab("runs");
  openAdminDialog("run", button.dataset.openRun);
}));
document.querySelectorAll("[data-open-user]").forEach((button) => button.addEventListener("click", () => openAdminDialog("user", button.dataset.openUser)));
$("#admin-dialog-close").addEventListener("click", () => $("#admin-dialog").close());
$("#admin-dialog-cancel").addEventListener("click", () => $("#admin-dialog").close());

document.querySelectorAll("[data-workspace-tab]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-workspace-tab]").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll("[data-panel]").forEach((panel) => panel.classList.add("hidden"));
  button.classList.add("active");
  document.querySelector(`[data-panel="${button.dataset.workspaceTab}"]`).classList.remove("hidden");
}));
$("#workspace-add-context").addEventListener("click", () => openDrawer("#materials-panel"));
$("#download-idml").addEventListener("click", () => {
  const content = `<?xml version="1.0" encoding="UTF-8"?>\n<idPkg:Story xmlns:idPkg="http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"><Story Self="demo"><Content>Демонстрационный макет книги «Город между строк». Не предназначен для реальной печати.</Content></Story></idPkg:Story>`;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([content], { type: "application/xml" }));
  link.download = "Город-между-строк-demo.idml";
  link.click();
  URL.revokeObjectURL(link.href);
});

$("#project-name").value = state.projectName;
$("#project-goal").value = state.projectGoal;
$("#project-context").value = state.context;
renderMaterials();
renderProfile();
const savedPage = localStorage.getItem("ct-page");
if (savedPage && document.getElementById(savedPage) && !["result", "next"].includes(savedPage)) showPage(savedPage, false);

} catch (error) {
  document.documentElement.dataset.appError = `${error.name}: ${error.message}`;
  console.error("CoreTeams prototype failed to initialize", error);
}
})();
