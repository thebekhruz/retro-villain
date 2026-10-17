import test from 'node:test';
import assert from 'node:assert/strict';

// Переводчик ждёт документ только для запуска; для чистой функции хватает заглушки.
globalThis.document = {readyState: 'loading', addEventListener() {}};
await import('../../retro/static/i18n-uz.js');
await import('../../retro/static/i18n.js');
const {translate} = globalThis.RetroI18n;

test('экран из редизайна переводится целиком, без смеси языков', () => {
  assert.equal(translate('Финансы дня'), 'Kunlik moliya');
  assert.equal(translate('Записать три покупки'), 'Uchta xaridni yozish');
  assert.equal(translate('День выплат'), "To'lov kuni");
});

test('дни недели и месяцы без числа', () => {
  assert.equal(translate('Сб'), 'Sh');
  assert.equal(translate('Суббота, 26 сентября'), 'Shanba, 26 sentabr');
  assert.equal(translate('РАСХОДЫ ОКТЯБРЯ'), 'OKTABR XARAJATLARI');
  assert.equal(translate('РАСХОДЫ МЕСЯЦА'), 'OY XARAJATLARI');
  // «Все» начинается с «Вс», но это не воскресенье.
  assert.equal(translate('Всего'), 'Jami');
});

test('единицы после чисел', () => {
  assert.equal(translate('5,9 млн'), '5,9 mln');
  assert.equal(translate('181 шт'), '181 dona');
  assert.equal(translate('31 из ≈147 чеков'), '31 / ≈147 chek');
});

test('шаблоны со своим порядком слов и неразрывными пробелами в суммах', () => {
  assert.equal(translate('39% выручки Retro'), 'Retro tushumining 39%');
  assert.equal(translate('Передано 0 при расчёте 14 723 000'), "Topshirildi 0, hisob bo'yicha 14 723 000");
  assert.equal(translate('3 замечания'), '3 ta izoh');
});

test('советы директора переводятся, название блюда остаётся как в iiko', () => {
  assert.equal(
    translate('«Сет Ретро на 5-6 чел.» хорошо продаётся, но маржа низкая — не ставьте его в акции.'),
    "«Сет Ретро на 5-6 чел.» yaxshi sotilmoqda, lekin marjasi past — uni aksiyaga qo'ymang.");
});

test('вход и отказы сервера переводятся целиком', () => {
  assert.equal(translate('Неверный логин или пароль.'), "Login yoki parol noto'g'ri.");
  assert.equal(translate('Эта панель недоступна для вашей учётной записи.'), 'Bu panel sizning hisobingiz uchun ochiq emas.');
  assert.equal(translate('Укажите цену числом.'), 'Narxni raqam bilan kiriting.');
  assert.equal(translate('Слишком большая сумма.'), 'Summa juda katta.');
});

test('подписи с данными внутри — шаблонами, данные не трогаем', () => {
  assert.equal(translate('Удалить расход «Хлеб для зала»'), "«Хлеб для зала» xarajatini o'chirish");
  assert.equal(
    translate('Оплаты расходятся с выручкой на 12 500 сум. Данные не считаются сверенными.'),
    "To'lovlar tushumdan 12 500 so'mga farq qiladi. Ma'lumotlar solishtirilgan hisoblanmaydi.");
  assert.equal(translate('Новые типы оплаты iiko показаны отдельно: Перечисления.'),
    "iiko'ning yangi to'lov turlari alohida ko'rsatilgan: Перечисления.");
});

test('T-402: непосчитанные предоплаты объясняются и по-узбекски', () => {
  assert.equal(translate('требует проверки'), 'tekshirish kerak');
  // Причина склеена из сообщения сервера и постоянной фразы — по кускам.
  assert.equal(
    translate('Кассовая смена ушла в минус (возврат аванса) — предоплаты за день не посчитать.'
              + ' Выручка и чеки за день верны.'),
    "Kassa smenasi minusga ketdi (oldindan to'lov qaytarilgan) — kunlik oldindan to'lovlarni"
    + " hisoblab bo'lmaydi. Kunlik tushum va cheklar to'g'ri.");
  assert.equal(translate('iiko не вернул кассовую смену за этот день — предоплаты не посчитать.'),
    "iiko bu kun uchun kassa smenasini qaytarmadi — oldindan to'lovlarni hisoblab bo'lmaydi.");
});

test('T-428: отказ без кассы дня называет даты и по-узбекски', () => {
  assert.equal(
    translate('Нет суммы от кассира за 09.10 (касса за 08.10). Впишите её в «Финансах дня» за 09.10 и повторите.'),
    "09.10 uchun kassirdan summa yo'q (08.10 kassasi). Uni «Kunlik moliya»da 09.10 kuniga yozing va qayta urinib ko'ring.");
});

test('T-429: посещаемость смены в клетке «Зарплаты · день» и должности группы', () => {
  assert.equal(translate('Смена 08.10: пришёл 09:31, вовремя'), "08.10 smenasi: 09:31 da keldi, o'z vaqtida");
  assert.equal(translate('Смена 08.10: пришёл 10:58, опоздал'), '08.10 smenasi: 10:58 da keldi, kechikdi');
  assert.equal(translate('Смена 07.10: не пришёл · выдано 360 000 сум'), "07.10 smenasi: kelmadi · 360 000 so'm berildi");
  assert.equal(translate('Смена 07.10: нет привязки к Hikvision'), "07.10 smenasi: Hikvision'ga bog'lanmagan");
  assert.equal(translate('нет'), "yo'q");
  assert.equal(translate('был'), 'keldi');
  assert.equal(translate('Все должности'), 'Barcha lavozimlar');
  assert.equal(translate('Повар тандыр'), 'Oshpaz tandir');
  assert.equal(translate('Лепёшка тандырная'), null, 'слово внутри другого слова не трогаем');
});

test('T-434: временный и период работы — по-узбекски, данные не трогаем', () => {
  assert.equal(translate('временный · 08.10–10.10'), 'vaqtinchalik · 08.10–10.10');
  assert.equal(translate('временный · с 08.10'), 'vaqtinchalik · 08.10 dan');
  assert.equal(translate('временный · по 10.10'), 'vaqtinchalik · 10.10 gacha');
  assert.equal(translate('Работает 08.10–10.10'), '08.10–10.10 ishlaydi');
  assert.equal(translate('Работает с 08.10'), '08.10 dan ishlaydi');
  assert.equal(translate('по 10.10'), '10.10 gacha', 'период отдельным узлом в строке ведомости');
  assert.equal(translate('Карамат работает с 08.10 по 10.10 — смену 12.10 отметить нельзя.'),
    "Карамат 08.10 dan 10.10 gacha ishlaydi — 12.10 smenasini belgilab bo'lmaydi.");
  assert.equal(translate('Карамат работает по 10.10 — доп. выплату за смену 11.10 записать нельзя.'),
    "Карамат 10.10 gacha ishlaydi — 11.10 smenasi uchun qo'shimcha to'lovni yozib bo'lmaydi.");
  assert.equal(translate('Карамат работает только 08.10 — выплату за смену 09.10 записать нельзя.'),
    "Карамат faqat 08.10 ishlaydi — 09.10 smenasi uchun to'lovni yozib bo'lmaydi.");
  assert.equal(translate('Период: — → с 08.10'), 'Davr: — → 08.10 dan');
  assert.equal(translate('Период не изменить: вне новых дат уже есть начисления или выплаты за смены 10.10; '
    + 'отметки «был / не был» за 12.10. Сначала уберите их или выберите другие даты.'),
  "Davrni o'zgartirib bo'lmaydi: yangi sanalardan tashqarida allaqachon 10.10 smenalari uchun hisoblash yoki to'lovlar; "
    + "12.10 uchun «keldi / kelmadi» belgilari bor. Avval ularni olib tashlang yoki boshqa sanalarni tanlang.");
  assert.equal(translate('Вне этих дней человека не будет в ведомости, и выплату ему не записать.'),
    "Bu kunlardan tashqarida odam qaydnomada bo'lmaydi va unga to'lov yozib bo'lmaydi.");
  assert.equal(translate('Период работы: «по» (08.10) раньше, чем «с» (10.10).'),
    'Ish davri: tugash sanasi (08.10) boshlanish sanasi (10.10) dan oldin.');
});

test('T-428: shift and payout dates remain distinct in Uzbek labels', () => {
  assert.equal(translate('За смену 09.10'), '09.10 smenasi uchun');
  assert.equal(translate('Обычная выплата 10.10'), 'Odatiy to‘lov 10.10');
  assert.equal(translate('выплата 10.10'), 'to‘lov 10.10');
  assert.equal(translate('Смена 09.10 · выплата 10.10'), 'Smena 09.10 · to‘lov 10.10');
  assert.equal(translate('Али · Смена 09.10 · выплата 10.10 · выдано 150000 сум'),
    'Али · Smena 09.10 · to‘lov 10.10 · 150000 so‘m berildi');
  assert.equal(translate('Доп. выплаты за смены · октябрь'), 'Smenalar uchun qo‘shimcha to‘lovlar · oktabr');
});

test('советы дня директора и их основание переводятся целиком (T-489)', () => {
  assert.equal(translate('Продвигайте «Кебаб»: на нём Retro зарабатывает больше всего.'),
    "«Кебаб»ni targ'ib qiling: Retro undan eng ko'p ishlaydi.");
  assert.equal(translate('Основание: маржа 67%, 40 шт и 3 млн сум · 9 — 15 октября 2026'),
    "Asos: marja 67%, 40 dona va 3 mln so'm · 9 — 15 oktabr 2026");
  assert.equal(translate('Основание: 2 шт · 9 — 15 октября 2026'), 'Asos: 2 dona · 9 — 15 oktabr 2026');
});

test('T-494: прогноз, блюда и закуп учредителя — по-узбекски', () => {
  assert.equal(translate('Среднее за 7–8 недель по кассам Retro и Oxbridge, без банкетного зала. Сегодня выделен.'),
    "Retro va Oxbridge kassalari bo'yicha 7–8 haftalik o'rtacha, banket zalisiz. Bugun ajratib ko'rsatilgan.");
  assert.equal(translate('10.10–16.10 · по прибыли: выручка минус себестоимость iiko'),
    "10.10–16.10 · foyda bo'yicha: tushumdan iiko tannarxi ayrilgan");
  assert.equal(translate('Закуп · уголь'), "Xarid · ko'mir");
  assert.equal(translate('Переводы (Oxbridge, блогеры)'), "O'tkazmalar (Oxbridge, blogerlar)");
  assert.equal(translate('Покупки с телефона · '), 'Telefondan xaridlar · ');
});

test('T-497: недельный порог Счёта Шефа и уведомление учредителю', () => {
  assert.equal(translate('Порог недели'), 'Haftalik chegara');
  assert.equal(translate('12,4 млн из 30 млн'), '12,4 mln / 30 mln');
  assert.equal(translate('950\u00a0000 из 30 млн'), '950\u00a0000 / 30 mln');
  assert.equal(translate('Порог 30 млн за неделю достигнут'), 'Haftalik 30 mln chegarasiga yetildi');
  assert.equal(translate('При 30 млн за неделю придёт сообщение в Telegram'),
    'Haftada 30 mln bo‘lganda Telegram’ga xabar keladi');
  assert.equal(translate('Уведомление отправлено 21.10 10:15'), 'Bildirishnoma yuborildi: 21.10 10:15');
  assert.equal(translate('Отмечаем счета больше 400\u00a0000'), '400\u00a0000 dan katta hisoblarni belgilaymiz');
  assert.equal(translate('3 выше 400\u00a0000'), '400\u00a0000 dan yuqori: 3');
  assert.equal(translate('Получатель уведомления не настроен'), 'Bildirishnoma oluvchisi sozlanmagan');
});

test('T-496: аналитика учредителя — столбцы, брони, источники и повтор рассылки по-узбекски', () => {
  assert.equal(translate('Неполная неделя: 3 дня из 7'), "To'liq bo'lmagan hafta: 7 kundan 3 kun");
  assert.equal(translate('За 90 дней столбцы собраны по неделям.'), "90 kun uchun ustunlar haftalar bo'yicha yig'ilgan.");
  assert.equal(translate('Retro в школе: 1 200 сум'), "Maktabdagi Retro: 1 200 so'm");
  assert.equal(translate('в том числе Яндекс Еда'), 'shu jumladan Яндекс Еда');
  assert.equal(translate('Гости по чекам iiko'), "iiko cheklari bo'yicha mehmonlar");
  assert.equal(translate('12 заявок'), '12 ta ariza');
  assert.equal(translate('Telegram с сайта · первый экран'), 'Saytdan Telegram · birinchi ekran');
  assert.equal(translate('Telegram · метка «flyer_oct»'), 'Telegram · «flyer_oct» belgisi');
  assert.equal(translate('Этот текст уже отправлен этим получателям 12 минут назад.'),
    'Bu matn shu oluvchilarga 12 daqiqa oldin yuborilgan.');
  assert.equal(translate('Отправить ещё раз'), 'Yana yuborish');
});
