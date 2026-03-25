"""Парсер владельцев карт с НЕМЕДЛЕННЫМ ПРЕРЫВАНИЕМ при буст/смене карты."""

import random
import re
import time
from typing import Callable, Dict, List, Optional, Tuple, Set
import requests
from bs4 import BeautifulSoup
from config import (
    BASE_URL,
    REQUEST_TIMEOUT,
    PAGE_DELAY,
    MIN_TRADE_DELAY,
    TRADE_RANDOM_DELAY_MIN,
    TRADE_RANDOM_DELAY_MAX,
    FIRST_PAGE_SKIP_OWNERS
)
from trade import TradeManager
from blacklist import get_blacklist_manager


def get_cards_to_send_count(boost_card: dict) -> int:
    """
    Определяет сколько карт нужно отправить в одном обмене.

    Логика:
    - owners <= 108               → 1 карта (замена карты клуба, см. card_replacement.py)
    - 109–216 owners + 121+ want  → 2 карты
    - 217–360 owners + 181+ want  → 2 карты
    - 361–540 owners + 300+ want  → 2 карты
    - иначе                       → 1 карта

    Returns:
        1 или 2
    """
    owners  = boost_card.get("owners_count", 0)
    wanters = boost_card.get("wanters_count", 0)

    if 109 <= owners <= 216 and wanters >= 121:
        return 2
    if 217 <= owners <= 360 and wanters >= 181:
        return 2
    if 361 <= owners <= 540 and wanters >= 300:
        return 2
    return 1


class Owner:
    """Класс владельца карты."""

    def __init__(self, owner_id: str, name: str, instance_id: Optional[int] = None):
        self.id = owner_id
        self.name = name
        self.instance_id: Optional[int] = instance_id

    def to_dict(self) -> Dict[str, str]:
        return {"id": self.id, "name": self.name, "instance_id": self.instance_id}


class OwnersParser:
    """Парсер для поиска владельцев карт."""

    def __init__(self, session: requests.Session):
        self.session = session
        self.blacklist_manager = get_blacklist_manager()

    def _extract_user_id(self, owner_element) -> Optional[str]:
        href = owner_element.get("href", "")
        match = re.search(r"/users/(\d+)", href)
        return match.group(1) if match else None

    def _extract_card_user_id(self, owner_element) -> Optional[int]:
        href = owner_element.get("href", "")
        match = re.search(r"card_user_id=(\d+)", href)
        return int(match.group(1)) if match else None

    def _extract_user_name(self, owner_element) -> str:
        name_elem = owner_element.select_one(".card-show__owner-name")
        return name_elem.get_text(strip=True) if name_elem else "Неизвестно"

    def _is_owner_available(self, owner_element) -> bool:
        owner_classes = owner_element.get("class", [])

        if "card-show__owner--online" not in owner_classes:
            return False

        lock_icons = owner_element.select(
            ".card-show__owner-icon--trade-lock .icon-lock, "
            ".card-show__owner-icon .icon-lock"
        )
        if lock_icons:
            return False

        handshake_icons = owner_element.select(
            ".card-show__owner-icon--block .icon-handshake, "
            ".card-show__owner-icon .icon-handshake"
        )
        if handshake_icons:
            return False

        return True

    def find_owners_on_page(
        self,
        card_id: str,
        page: int = 1,
    ) -> Tuple[List[Owner], bool]:
        url = f"{BASE_URL}/cards/{card_id}/users"
        if page > 1:
            url += f"?page={page}"

        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                return [], False

            soup = BeautifulSoup(response.text, "html.parser")
            owner_elements = soup.select(".card-show__owner")

            if not owner_elements:
                return [], False

            start_index = FIRST_PAGE_SKIP_OWNERS if page == 1 else 0
            available_owners: List[Owner] = []

            for idx, owner_elem in enumerate(owner_elements):
                if page == 1 and idx < start_index:
                    continue

                if not self._is_owner_available(owner_elem):
                    continue

                user_id = self._extract_user_id(owner_elem)
                if not user_id:
                    continue

                if self.blacklist_manager.is_blacklisted(user_id):
                    continue

                user_name = self._extract_user_name(owner_elem)
                instance_id = self._extract_card_user_id(owner_elem)

                available_owners.append(Owner(user_id, user_name, instance_id))

            has_next = self._has_next_page(soup)
            return available_owners, has_next

        except requests.RequestException:
            return [], False

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        for link in soup.select(".pagination__button a"):
            if link.get_text(strip=True) == "Вперёд":
                return True
        return False


class OwnersProcessor:
    """Процессор для обработки владельцев с НЕМЕДЛЕННЫМ ПРЕРЫВАНИЕМ."""

    MAX_RETRY_ATTEMPTS = 2

    def __init__(
        self,
        session: requests.Session,
        select_card_func: Callable,
        send_trade_func: Optional[Callable] = None,
        dry_run: bool = True,
        debug: bool = False,
    ):
        self.session = session
        self.parser = OwnersParser(session)
        self.select_card_func = select_card_func
        self.send_trade_func = send_trade_func
        self.dry_run = dry_run
        self.debug = debug
        self.last_trade_time = 0.0
        self.trade_manager = TradeManager(session, debug) if not dry_run else None
        self.failed_attempts_set: Set[int] = set()
        self.blacklist_manager = get_blacklist_manager()

    def reset_state(self) -> None:
        """Сбрасывает состояние процессора при смене карты."""
        if self.trade_manager:
            self.trade_manager.clear_sent_trades()
        self.last_trade_time = 0.0
        self.failed_attempts_set.clear()

    def _wait_before_trade(self) -> None:
        if self.dry_run:
            return
        current_time = time.time()
        time_since_last = current_time - self.last_trade_time
        if time_since_last < MIN_TRADE_DELAY:
            time.sleep(MIN_TRADE_DELAY - time_since_last)

    def _add_random_delay(self) -> None:
        if not self.dry_run:
            time.sleep(random.uniform(TRADE_RANDOM_DELAY_MIN, TRADE_RANDOM_DELAY_MAX))

    # ------------------------------------------------------------------
    # Проверка прерывания от монитора
    # ------------------------------------------------------------------

    def _check_interruption(self, monitor_obj, context: str = "") -> bool:
        if not monitor_obj:
            return False

        if not hasattr(monitor_obj, "should_interrupt"):
            return monitor_obj.card_changed if hasattr(monitor_obj, "card_changed") else False

        if monitor_obj.should_interrupt():
            reason = monitor_obj.get_interrupt_reason()
            print(f"\n⚡ ПРЕРЫВАНИЕ {context}: {reason}!")
            return True

        return False

    # ------------------------------------------------------------------
    # Подбор нескольких карт для одного обмена
    # ------------------------------------------------------------------

    def _select_cards_for_trade(
        self,
        boost_card: Dict,
        output_dir: str,
        cards_needed: int,
        base_exclude: Set[int],
    ) -> List[Dict]:
        """
        Выбирает cards_needed карт одного ранга для обмена.

        Каждая следующая карта исключает уже выбранные instance_id,
        чтобы не отправить одну и ту же карту дважды.

        Returns:
            Список выбранных карт (может быть меньше cards_needed если инвентарь заканчивается).
        """
        selected = []
        exclude = base_exclude.copy()

        for _ in range(cards_needed):
            card = self.select_card_func(
                self.session,
                boost_card,
                output_dir,
                trade_manager=self.trade_manager,
                exclude_instances=exclude,
            )
            if not card:
                break
            selected.append(card)
            # Исключаем только что выбранную карту для следующей итерации
            if card.get("instance_id"):
                exclude.add(card["instance_id"])

        return selected

    # ------------------------------------------------------------------
    # Обработка одного владельца
    # ------------------------------------------------------------------

    def process_owner_with_retry(
        self,
        owner: Owner,
        boost_card: Dict,
        output_dir: str,
        his_card_id: int,
        index: int,
        total: int,
        monitor_obj=None,
    ) -> tuple[bool, bool]:
        """
        Обрабатывает владельца с до MAX_RETRY_ATTEMPTS попытками.

        Количество карт в обмене определяется автоматически через
        get_cards_to_send_count(boost_card): 1 или 2 карты.

        Returns:
            (успех обмена, нужно прервать обработку)
        """
        if self.blacklist_manager.is_blacklisted(owner.id):
            return False, False

        # Сколько карт отправляем в этом обмене
        cards_needed = get_cards_to_send_count(boost_card)

        # Проверка #1: перед началом обработки владельца
        if self._check_interruption(monitor_obj, f"перед владельцем {owner.name}"):
            return False, True

        exclude_instances = self.failed_attempts_set.copy()

        for attempt in range(1, self.MAX_RETRY_ATTEMPTS + 1):
            # Проверка #2: перед каждой попыткой
            if self._check_interruption(monitor_obj, f"перед попыткой {attempt}/{self.MAX_RETRY_ATTEMPTS}"):
                return False, True

            selected_cards = self._select_cards_for_trade(
                boost_card, output_dir, cards_needed, exclude_instances
            )

            if not selected_cards:
                msg = "❌ Не удалось подобрать карту" if attempt == 1 else "❌ Карт больше нет"
                print(f"   [{index}/{total}] {owner.name} → {msg}")
                return False, False

            # Формируем строку для лога
            if len(selected_cards) == 1:
                c = selected_cards[0]
                trade_label = f"{c['name']} ({c['wanters_count']} желающих)"
            else:
                parts = ", ".join(
                    f"{c['name']} ({c['wanters_count']}♥)" for c in selected_cards
                )
                trade_label = f"2 карты: {parts}"

            if attempt == 1:
                print(f"   [{index}/{total}] {owner.name} → {trade_label}")
            else:
                print(f"      Попытка {attempt}/{self.MAX_RETRY_ATTEMPTS}: {trade_label}")

            my_instance_ids = [c["instance_id"] for c in selected_cards if c.get("instance_id")]
            my_card_name    = ", ".join(c.get("name", "") for c in selected_cards)
            my_wanters      = selected_cards[0].get("wanters_count", 0)

            if not self.send_trade_func or not my_instance_ids:
                return False, False

            self._wait_before_trade()

            # Проверка #3: перед отправкой обмена
            if self._check_interruption(monitor_obj, "перед отправкой обмена"):
                return False, True

            success = self.send_trade_func(
                session=self.session,
                owner_id=int(owner.id),
                owner_name=owner.name,
                my_instance_ids=my_instance_ids,       # список (1 или 2 карты)
                his_card_id=his_card_id,
                his_instance_id=owner.instance_id,
                my_card_name=my_card_name,
                my_wanters=my_wanters,
                trade_manager=self.trade_manager,
                dry_run=self.dry_run,
                debug=self.debug,
            )

            if success:
                if not self.dry_run:
                    self.last_trade_time = time.time()
                    self._add_random_delay()
                self.failed_attempts_set.clear()
                return True, False
            else:
                # Добавляем ВСЕ отправленные карты в failed_attempts_set
                for iid in my_instance_ids:
                    self.failed_attempts_set.add(iid)
                    exclude_instances.add(iid)

                if attempt < self.MAX_RETRY_ATTEMPTS:
                    print(f"      ⚠️  Попытка {attempt} не удалась, пробуем другие карты...")
                    time.sleep(1)
                else:
                    print(f"      ❌ Все {self.MAX_RETRY_ATTEMPTS} попытки исчерпаны")

        return False, False

    # ------------------------------------------------------------------
    # Постраничная обработка
    # ------------------------------------------------------------------

    def process_page_by_page(
        self,
        card_id: str,
        boost_card: Dict,
        output_dir: str,
        monitor_obj=None,
    ) -> int:
        """Обрабатывает владельцев постранично."""
        total_processed = 0
        total_trades_sent = 0
        page = 1

        # Показываем режим отправки карт
        cards_per_trade = get_cards_to_send_count(boost_card)
        owners = boost_card.get("owners_count", 0)
        wanters = boost_card.get("wanters_count", 0)

        print(f"🔍 Поиск доступных владельцев карты {card_id}...")
        print(f"📊 Режим: {'DRY-RUN (тестовый)' if self.dry_run else 'БОЕВОЙ (реальные обмены)'}")
        print(
            f"🃏 Карт в обмене: {cards_per_trade} "
            f"(владельцев буст-карты: {owners}, желающих: {wanters})"
        )

        blacklist_info = self.blacklist_manager.get_blacklist_info()
        if blacklist_info["count"] > 0:
            print(f"🚫 Черный список активен: {blacklist_info['count']} пользователей")

        if monitor_obj and hasattr(monitor_obj, "should_interrupt"):
            print("🔔 Мониторинг активен: будет прерывать при буст/смене карты")

        print()

        while True:
            # Проверка #1: перед загрузкой каждой страницы
            if self._check_interruption(monitor_obj, f"перед страницей {page}"):
                return total_processed

            owners_list, has_next = self.parser.find_owners_on_page(card_id, page)

            if owners_list:
                with_instance = sum(1 for o in owners_list if o.instance_id is not None)
                print(
                    f"📊 Страница {page}: найдено владельцев — {len(owners_list)} "
                    f"(instance_id: {with_instance}/{len(owners_list)})"
                )

                for idx, owner in enumerate(owners_list, 1):
                    # Проверка #2: перед каждым владельцем
                    if self._check_interruption(
                        monitor_obj,
                        f"перед владельцем {idx}/{len(owners_list)} на странице {page}",
                    ):
                        return total_processed

                    success, should_break = self.process_owner_with_retry(
                        owner,
                        boost_card,
                        output_dir,
                        int(card_id),
                        idx,
                        len(owners_list),
                        monitor_obj,
                    )

                    if should_break:
                        print("\n⚡ Прерывание обработки для перезапуска с новой картой!")
                        return total_processed

                    if success:
                        total_trades_sent += 1

                total_processed += len(owners_list)
                print()
            else:
                print(f"📊 Страница {page}: подходящих владельцев — 0\n")

            if not has_next:
                print(f"   Проверено владельцев: {total_processed}")
                print(f"   Отправлено обменов: {total_trades_sent}")
                break

            # Проверка #3: перед переходом на следующую страницу
            if self._check_interruption(
                monitor_obj,
                f"перед переходом со страницы {page} на {page + 1}",
            ):
                return total_processed

            time.sleep(PAGE_DELAY)
            page += 1

        return total_processed


def process_owners_page_by_page(
    session: requests.Session,
    card_id: str,
    boost_card: Dict,
    output_dir: str,
    select_card_func: Callable,
    send_trade_func: Optional[Callable] = None,
    monitor_obj=None,
    processor: Optional["OwnersProcessor"] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> int:
    if not processor:
        processor = OwnersProcessor(
            session=session,
            select_card_func=select_card_func,
            send_trade_func=send_trade_func,
            dry_run=dry_run,
            debug=debug,
        )

    return processor.process_page_by_page(
        card_id=card_id,
        boost_card=boost_card,
        output_dir=output_dir,
        monitor_obj=monitor_obj,
    )