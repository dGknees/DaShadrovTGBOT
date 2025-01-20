
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_gigachat.chat_models import GigaChat
from langchain import PromptTemplate
from aiogram import Bot, Dispatcher, types
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup, CallbackQuery
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.types import BotCommand, BotCommandScopeDefault
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import asyncio
import logging
from aiogram.types import TelegramObject
from aiogram.dispatcher.middlewares.base import BaseMiddleware
import json
import os
from dotenv import load_dotenv

load_dotenv()


NOTIFICATIONS_FILE = "notifications.json"
## LLM
GigaChatKey = os.getenv('GigaChatKey')
chat = GigaChat(credentials=GigaChatKey,
                model='GigaChat:latest',
                verify_ssl_certs=False,
                max_tokens=200,
                temperature = 0.6
                )

ask_advice = PromptTemplate(
    input_variables=["electr_desc"],
    template="Пользователь просит у вас совет по поводу сбережения электроэнергии. Постарайтесь посоветовать ему что-то, дать ему совет по поводу"
             "установки уведомлений в определенное время, которые помогут сохранять ему электроэнергию. "
             "(Формат уведомлений - каждый месяц в ЧЧ:ММ, каждую неделю в ЧЧ:ММ, каждый день в ЧЧ:ММ). Пользователь описывает свои электроприборы в доме и их использование так:"
             "{electr_desc}. Не используй markdown, не используй форматирование текста, навроде **текст**"
)
extract_reminders = PromptTemplate(
    input_variables=["advice"],
    template="Пользователь получил следующий совет по экономии электроэнергии: '{advice}'. "
             "Постарайся изввлечь из него все советы по установке уведомлений формата: периодичность - каждый день, месяц неделю, ЧЧ:ММ (другие периодичности, навроде каждую пятницу"
             " тебе извлекать нельзя), Не используй markdown и форматирование текста, навроде **текст**. "
)
aproximate_economy = PromptTemplate(
    input_variables=["advice","kwh_price","prev_usage"],
    template="Пользователь получил совет по экономии электроэнергии в своём доме на этот месяц: '{advice}'. В прошлом"
             "месяце он использовал {prev_usage} квт/час электроэнергии. Стоимость За квт/час в его месте проживания {kwh_price}. Спрогнозируй экономию за следующий месяц"
             "при соблюдении всех советов. Не используй markdown, и форматирование текста, навроде **текст**."
)
##конец

# Инициализация планировщика задач
scheduler = AsyncIOScheduler()

# Хранилище напоминаний в памяти (для упрощения; в реальном проекте лучше хранить в базе данных)
user_notifications = {}
user_chats = {}

def load_notifications():
    global user_notifications
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as file:
            user_notifications = json.load(file)
            logging.info("Уведомления успешно загружены из файла.")
    except FileNotFoundError:
        logging.warning("Файл с уведомлениями не найден. Будет создан новый.")
        user_notifications = {}
    except json.JSONDecodeError:
        logging.error("Ошибка чтения файла с уведомлениями. Файл будет перезаписан.")
        user_notifications = {}


# Сохраняем уведомления в файл
def save_notifications():
    try:
        with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as file:
            json.dump(user_notifications, file, ensure_ascii=False, indent=4)
            logging.info("Уведомления успешно сохранены в файл.")
    except Exception as e:
        logging.error(f"Ошибка сохранения уведомлений: {e}")


# Восстанавливаем задачи в планировщике
def restore_scheduler():
    for chat_id, notifications in user_notifications.items():
        for notification in notifications:
            job_id = notification["job_id"]
            text = notification["text"]
            periodicity = notification["periodicity"]
            time = notification["time"]

            # Восстанавливаем CronTrigger
            hour, minute = map(int, time.split(":"))
            if periodicity == "daily":
                trigger = CronTrigger(hour=hour, minute=minute)
            elif periodicity == "weekly":
                trigger = CronTrigger(day_of_week="mon", hour=hour, minute=minute)
            elif periodicity == "monthly":
                trigger = CronTrigger(day=1, hour=hour, minute=minute)
            else:
                logging.warning(f"Неизвестная периодичность для уведомления {job_id}. Пропускаем.")
                continue

            # Добавляем задачу в планировщик
            scheduler.add_job(
                send_notification,
                trigger,
                args=(int(chat_id), text),
                id=job_id,
                replace_existing=True,
            )



# Состояния для FSM
class NotificationStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_periodicity = State()
    waiting_for_time = State()
    delete_notification = State()


# Клавиатура для выбора периода
def get_periodicity_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="Каждый день")
    builder.button(text="Каждую неделю")
    builder.button(text="Каждый месяц")
    builder.adjust(1)  # Устанавливаем в 1 ряд
    return builder.as_markup(resize_keyboard=True)



def load_user_data_from_file():
    file_name = "user_data.json"
    if os.path.exists(file_name):  # Проверяем, существует ли файл
        try:
            with open(file_name, "r", encoding="utf-8") as file:
                data = json.load(file)  # Загружаем данные из файла
                print("Данные пользователей успешно загружены.")
                return data
        except (json.JSONDecodeError, IOError) as e:
            print(f"Ошибка при чтении файла {file_name}: {e}")
    else:
        print(f"Файл {file_name} не найден. Будет создан новый файл при сохранении.")
    return {}  # Возвращаем пустой словарь, если файл отсутствует или поврежден

user_data = load_user_data_from_file()
print(user_data)
token = os.getenv("token")

button_texts = {
    "FAQ": ["Зачем нужен бот?", "Как начать работу с ботом?","Функционал бота"],
    "change_data": [
        {"text": "цена за квт/час", "callback_data": "kwh"},
        {"text": "опис./использ. эл-техники", "callback_data": "el_desc"},
        {"text": "квт/час за прошлый месяц","callback_data":"prev_usage"}
    ],
}

# Определяем состояния
class Form(StatesGroup):
    name = State()
    electr_desc = State()
    kwh_price = State()
    prev_month = State()
    kwh_change = State()
    electr_desc_change = State()
    prev_month_change = State()

class RegistrationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):  # Проверяем только сообщения
            user_id = str(event.from_user.id)
            state = data.get('state')  # Получаем состояние пользователя
            print(user_id)
            print(event.text in button_texts['FAQ'])
            if event.text == "/faq" or event.text in button_texts['FAQ']:
                return await handler(event, data)
            if user_id in user_data and event.text=="/start":
              await event.answer("Вы уже зарегистрированы.")
              return
            # Если пользователь не зарегистрирован
            if user_id not in user_data:

                # Если пользователь находится в процессе регистрации, пропускаем обработку
                if state is not None:
                    state_name = await state.get_state()
                    if state_name in [Form.name.state, Form.electr_desc.state, Form.kwh_price, Form.prev_month]:
                        return await handler(event, data)

                # Если это не команда /start, перенаправляем пользователя на регистрацию
                if event.text != "/start":
                    await event.answer("Вы не зарегистрированы! Пожалуйста, используйте команду /start для регистрации. Подробнее в /faq")
                    return  # Прерываем обработку
        return await handler(event, data)



# Включаем логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(RegistrationMiddleware())

# Словарь для текстов кнопок




@dp.message(CommandStart(), State(None))
async def start_handler(message: Message, state: FSMContext):
    await message.answer("Добро пожаловать! Как к вам обращаться? Разрешено вводить только буквы. ")
    await state.set_state(Form.name)

@dp.message(F.text, Form.name)
async def handle_name(message: Message, state: FSMContext):
    name = message.text
    if not name.isalpha():
        await message.answer("Разрешено вводить только буквы.")
        return
    await state.update_data(name=name)
    await state.set_state(Form.electr_desc)
    await message.answer("Пожалуйста, Опишите какие электроприборы есть у вас в доме. Как вы ими пользуетесь? "
                         "Какое энергопотребление у них? ")
@dp.message(F.text, Form.electr_desc)
async def handle_electr_desc(message:Message, state: FSMContext):
    electr_desc = message.text
    await state.update_data(electr_desc=electr_desc)
    await state.set_state(Form.prev_month)
    await message.answer("Сколько киловатт/час электроэнергии вы потратили в прошлом месяце? (введите только число).")

def save_user_data_to_file():
    with open("user_data.json", "w", encoding="utf-8") as file:
        json.dump(user_data, file, indent=4, ensure_ascii=False)

def make_float(text: str) -> float:
    text = text.strip()
    if not text:
        return False
    # Заменяем запятую на точку для корректной обработки float() в Python
    text_with_dot = text.replace(',', '.')
    try:
        float(text_with_dot)
        return float(text_with_dot)
    except ValueError:
        return 0

@dp.message(F.text, Form.prev_month)
async def handle_prev_month(message:Message, state: FSMContext):
    usage = make_float(message.text)
    if  usage <=0:
        await message.answer("Ожидался ввод числа, большего от нуля.")
        return
    await state.update_data(prev_month=usage)
    await state.set_state(Form.kwh_price)
    await message.answer("Сколько стоит киловатт/час электроэнергии в вашем месте проживания? Введите одно лишь число.")

@dp.message(F.text, Form.kwh_price)
async def handle_kwh_price(message:Message, state: FSMContext):
    price = make_float(message.text)
    if price <=0:
        await message.answer("Ожидался ввод числа, большего нуля.")
        return
    await state.update_data(kwh_price=price)
    data = await state.get_data()
    user_data[str(message.from_user.id)]={
        "name" : data.get("name"),
        "electr_desc" : data.get("electr_desc"),
        "kwh_price" : data.get("kwh_price"),
        "prev_month" : data.get("prev_month"),
        "check" : "yes",
    }
    save_user_data_to_file()
    await message.answer(f"Регистрация упешно пройдена, {data.get('name')}")
    await state.clear()

# Первое меню: клавиатура через список
@dp.message(Command("faq"))
async def faq_menu_handler(message: Message):
    buttons = [[KeyboardButton(text=text)] for text in button_texts["FAQ"]]
    keyboard = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True, input_field_placeholder="sus")
    await message.answer("Выберите вопрос из меню.", reply_markup=keyboard)

# Третье меню: inline-клавиатура
@dp.message(Command("change_data"))
async def menu_ch_handler(message: Message):
    buttons = [
        InlineKeyboardButton(text=btn["text"], url=btn.get("url"), callback_data=btn.get("callback_data"))
        for btn in button_texts["change_data"]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons])
    await message.answer("Меню 3 - выберите действие.", reply_markup=keyboard)

# Отвечаем на inline-кнопки

@dp.callback_query()
async def callback_inl_keyboards(callback_query: CallbackQuery, state: FSMContext):
    if callback_query.data=="kwh":
        await state.set_state(Form.kwh_change)
        await callback_query.message.answer("Введите новую цену за кВтч:")
    elif callback_query.data=="el_desc":
        await state.set_state(Form.electr_desc_change)
        await callback_query.message.answer("Пожалуйста, Опишите какие электроприборы есть у вас в доме. Как вы ими пользуетесь? "
                         "Какое энергопотребление у них? ")
    elif callback_query.data=="prev_usage":
        await state.set_state(Form.prev_month_change)
        await callback_query.message.answer("Сколько квт/час вы использовали за прошлый месяц? В ответ впишите лишь число")


@dp.message(F.text, Form.electr_desc_change)
async def change_desc_handler(message: Message, state: FSMContext):
    electr_desc = message.text
    user_data[str(message.from_user.id)]['electr_desc']=electr_desc
    save_user_data_to_file()
    await state.clear()
    await message.answer(f"Теперь ваша информация об электроприборах и их использовании выглядит вот так: \n"
                         f" {electr_desc}")

@dp.message(F.text, Form.kwh_change)
async def change_kwh_handler(message: Message, state: FSMContext):
    price = make_float(message.text)
    if price <=0:
        await message.answer("Ожидался ввод числа, большего нуля.")
        return
    user_data[str(message.from_user.id)]['kwh_price']=price
    save_user_data_to_file()
    await state.clear()
    await message.answer(f"Цена успешно изменена на {price}")

@dp.message(F.text, Form.prev_month_change)
async def change_prev_handler(message: Message, state: FSMContext):
    usage = make_float(message.text)
    if usage <= 0:
        await message.answer("Ожидался ввод числа, большего нуля.")
        return
    user_data[str(message.from_user.id)]['prev_month']=usage
    save_user_data_to_file()
    await state.clear()
    await message.answer(f"Электроэнергии использовано за последний месяц: {usage} квт/час")


@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await message.answer("Напишите текст уведомления:")
    await state.set_state(NotificationStates.waiting_for_message)

# Обработчик текста уведомления
@dp.message(NotificationStates.waiting_for_message)
async def notification_message_handler(message: types.Message, state: FSMContext):
    await state.update_data(notification_text=message.text)
    await message.answer(
        "Выберите периодичность уведомления:", reply_markup=get_periodicity_keyboard()
    )
    await state.set_state(NotificationStates.waiting_for_periodicity)  # Переходим к следующему состоянию

# Обработчик выбора периодичности
@dp.message(NotificationStates.waiting_for_periodicity)
async def notification_periodicity_handler(message: types.Message, state: FSMContext):
    periodicity_mapping = {
        "Каждый день": "daily",
        "Каждую неделю": "weekly",
        "Каждый месяц": "monthly",
    }

    selected_periodicity = periodicity_mapping.get(message.text)

    if selected_periodicity:
        await state.update_data(periodicity=selected_periodicity)
        await message.answer("Введите время в формате HH:MM (например, 09:30):")
        await state.set_state(NotificationStates.waiting_for_time)  # Переходим к следующему состоянию
    else:
        await message.answer("Некорректный выбор. Попробуйте снова, используя кнопки.")


# Обработчик ввода времени
@dp.message(NotificationStates.waiting_for_time)
async def notification_time_handler(message: types.Message, state: FSMContext):
    try:
        # Проверяем формат времени
        time_parts = message.text.split(":")
        if len(time_parts) != 2:
            raise ValueError("Неверный формат времени")

        hour = int(time_parts[0])
        minute = int(time_parts[1])

        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("Часы или минуты вне диапазона")

        # Получаем данные пользователя из состояния FSM
        user_data = await state.get_data()
        notification_text = user_data.get("notification_text")
        periodicity = user_data.get("periodicity")
        chat_id = message.chat.id

        # Настраиваем CronTrigger для планировщика
        if periodicity == "daily":
            trigger = CronTrigger(hour=hour, minute=minute)
        elif periodicity == "weekly":
            trigger = CronTrigger(day_of_week="mon", hour=hour, minute=minute)
        elif periodicity == "monthly":
            trigger = CronTrigger(day=1, hour=hour, minute=minute)
        else:
            raise ValueError("Неизвестная периодичность")

        # Создаем ID задачи
        job_id = f"notification_{chat_id}_{notification_text[:10]}"

        # Добавляем задачу в планировщик
        scheduler.add_job(
            send_notification,
            trigger,
            args=(chat_id, notification_text),
            id=job_id,
            replace_existing=True,
        )

        # Сохраняем уведомление в хранилище
        if str(chat_id) not in user_notifications:
            user_notifications[str(chat_id)] = []
        user_notifications[str(chat_id)].append({
            "job_id": job_id,
            "text": notification_text,
            "periodicity": periodicity,
            "time": message.text
        })
        save_notifications()

        # Уведомляем пользователя
        await message.answer(
            f"Уведомление добавлено! Текст: \"{notification_text}\". Время: {message.text}."
        )
        await state.clear()  # Очищаем состояние FSM
    except ValueError as e:
        await message.answer(f"Некорректный формат времени. Убедитесь, что вы ввели время в формате HH:MM. Ошибка: {e}")


# Обработчик команды /delete
@dp.message(Command("delete"))
async def cmd_delete(message: types.Message, state: FSMContext):
    chat_id = str(message.chat.id)
    if chat_id not in user_notifications or not user_notifications[chat_id]:
        await message.answer("У вас нет активных уведомлений.")
        return

    # Создаем список уведомлений для удаления
    keyboard = ReplyKeyboardBuilder()
    for notification in user_notifications[chat_id]:
        keyboard.button(text=notification["job_id"])
    keyboard.adjust(1)
    await state.set_state(NotificationStates.delete_notification)
    await message.answer(
        "Выберите уведомление для удаления:", reply_markup=keyboard.as_markup(resize_keyboard=True)
    )


# Обработчик для удаления уведомления
@dp.message(NotificationStates.delete_notification)
async def delete_notification_handler(message: types.Message, state: FSMContext):
    chat_id = str(message.chat.id)
    job_id = message.text

    if chat_id in user_notifications:
        for notification in user_notifications[chat_id]:
            if notification["job_id"] == job_id:
                scheduler.remove_job(job_id)
                user_notifications[chat_id].remove(notification)
                save_notifications()
                await message.answer(f"Уведомление \"{job_id}\" удалено.")
                break
        else:
            await message.answer("Уведомление не найдено.")
    else:
        await message.answer("Уведомление не найдено.")
    await state.clear()

# Функция отправки уведомления
async def send_notification(chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, f"🔔 Уведомление: {text}")
    except Exception as e:
        logging.error(f"Ошибка при отправке уведомления: {e}")

@dp.message(Command("reminders"))
async def show_rem_handler(message:Message, state: FSMContext):
    print(user_notifications)
    chat_id = message.chat.id
    reminders = "Ваши уведомления:\n"
    if not len(user_notifications[str(chat_id)]):
        await message.answer("У вас пока нет никаких уведомлений.")
    else:
        i = 1
        for rem in user_notifications[str(chat_id)]:
            reminders+= str(i)+". "+rem["periodicity"]+" "+rem["time"]+" "+rem["job_id"].split('_',2)[2]+'\n'
            i+=1
        await message.answer(reminders)


@dp.message(Command("advice"))
async def give_advice (message: types.Message, state: FSMContext):

    el_dsc = user_data[str(message.from_user.id)]["electr_desc"]
    kwh_price = user_data[str(message.from_user.id)]["kwh_price"]
    prev_usage = user_data[str(message.from_user.id)]["prev_month"]

    llm_chain = ask_advice | chat
    advice = llm_chain.invoke(el_dsc).content
    print(advice)
    await message.answer(advice)

    llm_chain = aproximate_economy | chat
    economy = llm_chain.invoke({"advice":advice,"kwh_price":kwh_price,"prev_usage":prev_usage}).content

    await message.answer(economy)

    llm_chain = extract_reminders | chat
    rems = llm_chain.invoke(advice).content

    await message.answer(rems)
    await message.answer("Был ли данный ответ полезен? Пожалуйста, оставьте отзыв")
    await state.set_state(State("review"))

@dp.message(F.text, State("review"))
async def get_review(message:Message, state: FSMContext):
    try:
        with open("reviews.json", "a", encoding="utf-8") as file:
            file.write(str(message.from_user.id)+" : "+message.text+'\n')
            logging.info("отзыв успешно сохранен в файл.")
            file.close()
            await message.answer("Спасибо за отзыв.")
            await state.clear()

    except Exception as e:
        logging.error(f"Ошибка сохранения отзыва: {e}")
        await state.clear()



@dp.message(Command("chat"))
async def chat_start(message:Message, state: FSMContext):
    user_chats[message.from_user.id]=[SystemMessage(content="Ты хорошо разбираешься в экономии электроэнергии и стараешься"
                                                           "дать совет об экономии электроэнергии пользоателю, тебе нельзя использовать markdown в своих ответах")]
    await state.set_state(State("chatting"))
    await message.answer("Чтобы остановить чат с ассистентом введите 'stop'")
    await message.answer("Чем я могу вам помочь?")

@dp.message(State("chatting"))
async def chat_handler(message:Message, state: FSMContext):
    if message.text=="stop":
        await state.clear()
        await message.answer("чат завершен")
        del user_chats[message.from_user.id]
        print(user_chats)
        return
    user_chats[message.from_user.id].append(HumanMessage(content=message.text))
    answer = chat.invoke(user_chats[message.from_user.id])
    user_chats[message.from_user.id].append(AIMessage(content=answer.content))
    await message.answer(answer.content)

@dp.message(Command("check"))
async def change_rem(message:Message):
    if user_data[str(message.from_user.id)]["check"]=="yes":
        user_data[str(message.from_user.id)]["check"] = "no"
        await message.answer("Уведомление о проверке счетчика выключено")
    else:
        user_data[str(message.from_user.id)]["check"] = "yes"
        await message.answer("Уведомление о проверке счетчика включено")
    save_user_data_to_file()

@dp.message(Command("my_data"))
async def my_data(message:Message):
    data = user_data[str(message.from_user.id)]["name"]+" - имя\n"
    data += str(user_data[str(message.from_user.id)]["kwh_price"])+" - цена за квт/час\n"
    data += str(user_data[str(message.from_user.id)]["prev_month"])+" - квт/час за прошлый месяц\n"
    data += user_data[str(message.from_user.id)]["electr_desc"]+" - описание пользования приборами\n"
    await message.answer(data)

@dp.message(F.text)
async def unreg_handler(message:Message, state: FSMContext):

    if message.text == "Зачем нужен бот?":
        await message.answer("Данный бот разработан с целью помочь людям эффективнее использовать электроэнергию у себя дома.")
        return
    elif message.text == "Как начать работу с ботом?":
        await message.answer("Необходимо написать /start для регистрации, после ее прохождения вы сможете пользоваться функционалом бота.")
        return
    elif message.text == "Функционал бота":
        await message.answer("/advice - Используя ввеленные вами данные, дает совет, как лучше экономить электроэнергию.\n"
                             "/add - добавить уведомление.\n"
                             "/delete - удалить уведомление.\n"
                             "/reminders - просмотр всех уведомлений.\n"
                             "/chat - запуск чата с ии ассистеннтом. \n"
                             "/check - выключает/выключает напоминание о проверке счетчика каждый месяц\n"
                             "/change_data - вызывает меню выбора данных пользователя для смены, запрашивает новые данные")
        return
    else:
        user_name = user_data.get(str(message.from_user.id)).get("name", "пользователь")
        await message.answer(f"Уважаемый {user_name}, используйте меню для взаимодействия с ботом.")

async def monthly_rem():
    for user_id in user_data.keys():
        if user_data[user_id]["check"]=="yes":
            # Устанавливаем состояние для пользователя через FSMContext
            state = dp.fsm.get_context(bot=bot, user_id=int(user_id), chat_id=int(user_id))
            await state.set_state(Form.prev_month_change)
            # Отправляем сообщение пользователю
            await bot.send_message(
                user_id,
                "Месяц подошел к концу, настало время проверить счетчик и записать его показания. /check чтобы вкл/выкл уведомления"
                "\n Введите сколько квт/час было использовано в прошлом месяце (вводить только число)\n p.s срабатывает каждые 30 секунд для проверки"
            )

# Установка команд бота
async def setup_commands():
    commands = [
        BotCommand(command="faq", description="Часто задаваемые вопросы"),
        BotCommand(command="change_data", description="Изменить мои данные"),
        BotCommand(command="delete", description="удалить уведомление"),
        BotCommand(command="add", description="добавить уведомление"),
        BotCommand(command="reminders", description="мои уведомления"),
        BotCommand(command="advice", description="Совет от ии ассистента"),
        BotCommand(command="chat", description="Чат с ии ассистентом"),
        BotCommand(command="check", description="вкл/выкл Напоминание о проверке счетчиков в начале месяца"),
        BotCommand(command="my_data", description="Просмотр ваших данных"),
    ]
    await bot.set_my_commands(commands, BotCommandScopeDefault())

# Основная функция запуска
async def main():
    dp.startup.register(setup_commands)  # Регистрация команд при запуске
    try:

        print("Бот запущен.")
        # Загружаем уведомления из JSON-файла
        load_notifications()

        # Восстанавливаем задачи в планировщике
        restore_scheduler()
        scheduler.add_job(monthly_rem, "interval", seconds=30)
        scheduler.start()
        await bot.delete_webhook(drop_pending_updates=True)  # Удаляем вебхуки, если они есть
        # Запускаем polling с указанными allowed_updates
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"])
    finally:
        await bot.session.close()  # Закрываем сессию бота при завершении
        print("Бот остановлен.")

# Запуск бота
asyncio.run(main())