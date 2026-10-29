import { useEffect, useState } from 'react';
import { api, type ProjectSummary } from './api';
import { errorText, projectRoute, useStartProject } from './actions';
import { MEDIA, useShell } from './context';
import { navigate } from './router';
import { useVideo } from './hooks';
import { Icon, PhaseTag } from './ui';

export function Home() {
  const start = useStartProject();
  const video = useVideo();
  return (
    <div className="page">
      <section className="home-hero">
        <div>
          <p className="home-kicker"><span className="line" />AI-команда вокруг вашей задачи</p>
          <h1 className="home-title">Ваша задача.<br />Нужная команда.<br /><span>Один результат.</span></h1>
          <p className="lead">Расскажите, что хотите сделать. CoreTeams подберёт специалистов, организует их работу и соберёт выводы в понятное решение.</p>
          <div className="actions">
            <button className="button primary" type="button" onClick={() => void start(false)}>Рассказать о задаче <Icon name="arrow" /></button>
            <button className="button" type="button" onClick={() => void start(true)}>Посмотреть на примере</button>
          </div>
          <p className="helper">Книга, учебный курс, бизнес-идея или другая задача.<br />Вы задаёте цель — мы предлагаем команду.</p>
        </div>
        <figure className="home-visual" style={{ margin: 0 }}>
          <img src={MEDIA.team} alt="Помощник CoreTeams и команда специалистов в общей мастерской" />
          <figcaption className="visual-caption">
            <div><p>Как задача становится результатом</p><small>Знакомство с CoreTeams · 75 секунд</small></div>
            <button className="play-button" type="button" onClick={() => video(0)} aria-label="Смотреть видео о CoreTeams"><Icon name="play" /></button>
          </figcaption>
        </figure>
      </section>
      <section className="home-how" aria-label="Как устроена работа">
        <article><span className="step-number">01</span><div><h3>Расскажите о задаче</h3><p>Помощник задаст вопросы и подскажет, какие материалы пригодятся.</p></div></article>
        <article><span className="step-number">02</span><div><h3>Познакомьтесь с командой</h3><p>Почему нужны эти роли, за что отвечает каждая и что получится вместе.</p></div></article>
        <article><span className="step-number">03</span><div><h3>Решите, что делать дальше</h3><p>Примите результат или попросите изменить. Проект можно продолжить позже.</p></div></article>
      </section>
      <p className="home-demo-note">Результаты пишет Claude по вашей задаче и материалам. Проекты сохраняются в вашем аккаунте.</p>
    </div>
  );
}

function BookCase() {
  return (
    <>
      <img src={MEDIA.book} alt="Разворот книги Город между строк" style={{ borderRadius: 10, marginBottom: 20 }} />
      <p>Вымышленная книга о городе, хранящем воспоминания жителей. Команда помогает связать структуру, редактуру и оформление.</p>
      <ul>
        <li>Редактор: структура глав и замечания к рукописи.</li>
        <li>Дизайнер: образец разворота и типографика.</li>
        <li>Верстальщик: подготовка макета для InDesign.</li>
      </ul>
      <p className="helper">Это демонстрация формата результата. Готовый печатный IDML сервис не создаёт.</p>
    </>
  );
}

function CoffeeCase() {
  return (
    <>
      <p>Одна идея требует нескольких точек зрения. CoreTeams организует их вокруг вашего решения.</p>
      <ol className="plan-timeline">
        <li><span className="num">1</span><div><h4>Исследователь</h4><p>Спрос, аудитория и конкуренты.</p></div></li>
        <li><span className="num">2</span><div><h4>Финансист и операционный специалист</h4><p>Расходы, нагрузка и условия окупаемости.</p></div></li>
        <li><span className="num">3</span><div><h4>Маркетолог</h4><p>Позиционирование и первые каналы привлечения.</p></div></li>
      </ol>
      <p>Итог — единый план проверки идеи с допущениями и открытыми вопросами.</p>
      <p className="helper">Для своей бизнес-задачи выберите направление «Бизнес и новая идея» при создании проекта. Команда подготовит план проверки по вашему описанию; исследование реального рынка она не проводит.</p>
    </>
  );
}

export function Catalog() {
  const start = useStartProject();
  const { openDialog } = useShell();
  return (
    <div className="page">
      <p className="eyebrow">Каким бывает результат</p>
      <h1>Одна задача. Несколько точек зрения.</h1>
      <p className="lead">Учебные примеры, чтобы понять формат работы. Это не реальные отзывы клиентов.</p>
      <div className="catalog-grid">
        <article className="catalog-card">
          <img className="cover" src={MEDIA.result} alt="Команда передаёт собранный документ" />
          <div className="catalog-content">
            <p className="eyebrow">Образование · интерактивный пример</p>
            <h3>Из теории — в практику</h3>
            <p>Карта проблем курса, новая программа и первое занятие для начинающих дизайнеров.</p>
            <button className="button primary" type="button" onClick={() => void start(true)}>Пройти этот пример</button>
          </div>
        </article>
        <article className="catalog-card">
          <img className="cover book" src={MEDIA.book} alt="Разворот демонстрационной книги Город между строк" />
          <div className="catalog-content">
            <p className="eyebrow">Книга · пример результата</p>
            <h3>«Город между строк»</h3>
            <p>Рукопись, редакционный план и оформление книжного разворота.</p>
            <button className="button" type="button" onClick={() => openDialog('«Город между строк»', <BookCase />)}>Посмотреть результат</button>
          </div>
        </article>
        <article className="catalog-card">
          <div className="cover cover-type" aria-hidden="true">☕</div>
          <div className="catalog-content">
            <p className="eyebrow">Малый бизнес · будущий сценарий</p>
            <h3>Кофейня у университета</h3>
            <p>Оценка идеи через спрос, экономику, продвижение и ограничения запуска.</p>
            <button className="button" type="button" onClick={() => openDialog('Команда для запуска кофейни', <CoffeeCase />)}>Что подготовит команда</button>
          </div>
        </article>
      </div>
    </div>
  );
}

export function Profile() {
  const start = useStartProject();
  const { toast } = useShell();
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .projects()
      .then((items) => {
        if (!cancelled) setProjects(items);
      })
      .catch((error: unknown) => {
        const message = errorText(error, 'Не удалось загрузить проекты');
        if (!cancelled && message) {
          toast(message);
          setProjects([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [toast]);

  return (
    <div className="page">
      <div className="profile-top">
        <div>
          <p className="eyebrow">Ваше пространство</p>
          <h1>Мои проекты</h1>
          <p className="quota">{projects ? `Проектов: ${projects.length}` : 'Загрузка…'}</p>
        </div>
        <button className="button primary" type="button" onClick={() => void start(false)}><Icon name="plus" /> Новый проект</button>
      </div>
      {projects && projects.length > 0 && (
        <div className="project-list">
          {projects.map((project) => (
            <article key={project.id} className="project-card">
              <div>
                <PhaseTag phase={project.phase} />
                <h3>{project.name || 'Новый проект'}</h3>
                <p>
                  {project.status === 'draft' ? 'Описание и подготовка задачи' : `Этап ${project.stage + 1} из 3 · ${project.stage_title}`}
                  {project.example ? ' · учебный пример' : ''}
                </p>
              </div>
              <button className="button primary" type="button" onClick={() => navigate(projectRoute(project))}>
                {['done', 'ready'].includes(project.status) ? 'Открыть результат' : 'Продолжить с этого места'}
              </button>
            </article>
          ))}
        </div>
      )}
      {projects && projects.length === 0 && (
        <div className="empty">
          <span className="empty-icon"><Icon name="file" /></span>
          <h3>Здесь появится ваш первый проект</h3>
          <p>Можно принести свою задачу или пройти готовый пример, чтобы познакомиться с процессом.</p>
          <button className="button primary" type="button" onClick={() => void start(true)}>Попробовать на примере</button>
        </div>
      )}
      <p className="helper spacer-top">Проекты сохраняются в вашем аккаунте. Результат каждого проекта можно скачать одним Markdown-файлом.</p>
      <details className="ct-advanced">
        <summary>Для опытных</summary>
        <p>Свободный диалог — прямая переписка с Claude без этапов и команды. Там же прежние диалоги, которые не относятся к проектам.</p>
        <a className="text-button" href="#chat">Открыть свободный диалог →</a>
      </details>
    </div>
  );
}

export function AboutDialog() {
  return (
    <>
      <p>CoreTeams проводит задачу по пути: задача → контекст → уточнения → предложение команды → план → работа → согласование → результат.</p>
      <div className="notice">Каждый из трёх результатов пишет Claude по вашей задаче, ответам и загруженным материалам. Команда — это схема ролей, по которой он работает; отдельные агенты CTF не запускаются.</div>
      <p className="spacer-top">Файлы передаются Claude в папку materials/ проекта. Стоимость и время работы показываются по фактическим запускам.</p>
      <p className="spacer-top">Проверяйте выводы: Claude не проводит исследование рынка и может ошибаться в фактах, которых нет в материалах.</p>
    </>
  );
}
