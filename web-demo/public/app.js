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
  materials: JSON.parse(localStorage.getItem("ct-materials") || "[]"),
  revision: false,
  credits: Number(localStorage.getItem("ct-credits") || 0)
};

const questions = [
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

const team = [
  ["Аналитик", "Уточняет потребность и критерий результата"],
  ["Дизайнер", "Собирает понятный пользовательский путь"],
  ["Архитектор", "Определяет границы первой версии"],
  ["Разработчик", "Создаёт согласованный результат"],
  ["Тестировщик", "Независимо проверяет работу"]
];

function showPage(id, remember = true) {
  pages.forEach((page) => page.classList.add("hidden"));
  const page = document.getElementById(id);
  if (!page) return;
  page.classList.remove("hidden");
  state.page = id;
  if (remember && !["signup", "development"].includes(id)) localStorage.setItem("ct-page", id);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderMaterials() {
  $("#material-count").textContent = state.materials.length;
  $("#material-list").innerHTML = state.materials.length
    ? state.materials.map((name) => `<li>${name}</li>`).join("")
    : "<li>Материалов пока нет.</li>";
  $("#summary-files").textContent = state.materials.length ? state.materials.join(", ") : "Не добавлены";
}

function openDrawer(id) {
  document.querySelectorAll(".drawer").forEach((drawer) => drawer.classList.add("hidden"));
  $(id).classList.remove("hidden");
}

function renderQuestion() {
  const current = questions[state.question];
  $("#question-title").textContent = current.title;
  $("#question-help").textContent = current.help;
  $("#answer-options").innerHTML = current.options.map((option) => `<button data-answer="${option}">${option}</button>`).join("");
  $("#question-next").classList.add("hidden");
  document.querySelectorAll("[data-answer]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-answer]").forEach((item) => item.classList.remove("selected"));
      button.classList.add("selected");
      state.answers[state.question] = button.dataset.answer;
      $("#question-next").classList.remove("hidden");
    });
  });
}

function fillSummary() {
  const [request, audience, outcome] = state.answers;
  $("#summary-result").textContent = request || "Формат уточнит команда";
  $("#summary-audience").textContent = audience || "Команда предложит аудиторию";
  $("#summary-outcome").textContent = outcome || "Критерий уточнит фасилитатор";
  const unknown = state.answers.filter((answer) => /не знаю|предполож/i.test(answer || "")).length;
  $("#summary-assumption").textContent = unknown
    ? unknown === 1
      ? "Команда предложит 1 безопасное предположение для подтверждения"
      : `Команда предложит ${unknown} безопасных предположения для подтверждения`
    : "Критичных предположений нет";
  renderMaterials();
}

function fillLanding() {
  const [request, audience, outcome] = state.answers;
  const proposedTitle = /не знаю|предлож/i.test(request || "")
    ? "Проверка идеи до больших вложений"
    : request?.replace(/^Есть /, "") || "Первая версия идеи";
  $("#landing-title").textContent = proposedTitle;
  $("#landing-copy").textContent = `Первая версия для аудитории: ${audience || "уточняется"}. Она поможет ${(outcome || "проверить ценность идеи").toLowerCase()}.`;
  $("#landing-audience").textContent = audience || "Аудитория уточняется";
}

function updateCredits(value) {
  state.credits = value;
  localStorage.setItem("ct-credits", String(value));
  $("#credits-used").textContent = (1000 - value).toLocaleString("ru-RU");
}

function runDevelopment(isRevision = false) {
  state.revision = isRevision;
  showPage("development", false);
  $("#credit-badge").classList.remove("hidden");
  $("#agent-list").innerHTML = "";
  $("#progress-bar").style.width = "0%";
  $("#progress-percent").textContent = "0%";
  $("#development-title").textContent = isRevision ? "Команда дорабатывает результат" : "Команда начинает работу";
  localStorage.setItem("ct-run", JSON.stringify({ status: "running", revision: isRevision, startedAt: Date.now() }));

  let index = 0;
  const selectedTeam = isRevision
    ? [["Фасилитатор", "Уточняет запрос на изменение"], ["Дизайнер", "Обновляет структуру результата"], ["Разработчик", "Вносит согласованные изменения"], ["Тестировщик", "Сравнивает новую версию с запросом"]]
    : team;

  const tick = () => {
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
      index += 1;
      setTimeout(tick, 720);
      return;
    }

    $("#progress-bar").style.width = "100%";
    $("#progress-percent").textContent = "100%";
    $("#facilitator-live-text").textContent = "Команда завершила работу. Общий результат сохранён.";
    updateCredits(isRevision ? Math.min(1000, state.credits) : 240);
    localStorage.setItem("ct-run", JSON.stringify({ status: "completed", revision: isRevision }));
    setTimeout(() => {
      $("#result-title").textContent = isRevision ? "Обновлённая версия результата" : "Первая версия результата";
      $("#result-copy").textContent = isRevision
        ? "Фасилитатор собрал запрос, нужные роли внесли изменения, тестировщик проверил новую версию. Предыдущая версия сохранена."
        : "Команда объединила анализ, пользовательский путь, архитектурные границы и проверку в один общий артефакт.";
      showPage("result");
    }, 600);
  };
  tick();
}

document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
$("#materials-toggle").addEventListener("click", () => openDrawer("#materials-panel"));
$("#notifications-toggle").addEventListener("click", () => openDrawer("#notifications-panel"));
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.close).classList.add("hidden")));
$("#global-files").addEventListener("change", (event) => {
  const names = [...event.target.files].map((file) => file.name);
  state.materials = [...new Set([...state.materials, ...names])];
  localStorage.setItem("ct-materials", JSON.stringify(state.materials));
  renderMaterials();
});
$("#play-demo").addEventListener("click", () => showPage("about"));
$("#begin").addEventListener("click", () => {
  showPage("signup", false);
});

$("#signup-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const password = $("#password").value;
  if (password !== $("#password-repeat").value) {
    $("#signup-error").textContent = "Пароли не совпадают.";
    return;
  }
  $("#signup-error").textContent = "";
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
  if (state.question < questions.length - 1) {
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
$("#make-landing").addEventListener("click", () => {
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
$("#accept-result").addEventListener("click", () => showPage("next"));
$("#scale").addEventListener("click", () => {
  $("#next-note").textContent = "Фасилитатор подготовит отдельный диалог о целях, ресурсах и границах роста. В демо процесс не запускается.";
});
$("#invest").addEventListener("click", () => {
  $("#next-note").textContent = "Сначала сервис запросит согласие на состав публичных материалов. В демо данные не публикуются и никому не передаются.";
});

renderMaterials();
const savedPage = localStorage.getItem("ct-page");
if (savedPage && document.getElementById(savedPage) && !["result", "next"].includes(savedPage)) showPage(savedPage, false);

} catch (error) {
  document.documentElement.dataset.appError = `${error.name}: ${error.message}`;
  console.error("CoreTeams prototype failed to initialize", error);
}
})();
