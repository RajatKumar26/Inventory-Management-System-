import json
import math
import os
import re
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import (
    Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence,
    Type, TypeVar,
)

# ---------------------------------------------------------------------------
# Configuration (change these values to customise the program)
# ---------------------------------------------------------------------------
DATA_FILE_NAME = "inventory.json"
CURRENCY = "Rs."
LOW_STOCK_THRESHOLD = 5  # "low stock" means fewer units than this
MAX_QUANTITY = 99_999
MAX_PRICE = 9_999_999.99
MAX_WARRANTY_MONTHS = 120
MAX_NAME_LENGTH = 22
MAX_ID_LENGTH = 8
DATE_FORMAT = "%Y-%m-%d"
ID_PATTERN = re.compile(rf"[A-Z0-9_-]{{1,{MAX_ID_LENGTH}}}")

# (title, width, alignment) for every column of the inventory table.
TABLE_COLUMNS = (
    ("ID", MAX_ID_LENGTH, "<"),
    ("Category", 11, "<"),
    ("Name", MAX_NAME_LENGTH, "<"),
    (f"Price ({CURRENCY})", 12, ">"),
    ("Qty", 5, ">"),
    ("Details", 20, "<"),
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class InventoryError(Exception):
    """Base class for every error raised by the inventory system."""


class ValidationError(InventoryError, ValueError):
    """Raised when a value is invalid (it is also a ``ValueError``)."""


class DuplicateProductError(InventoryError):
    """Raised when a product ID is already in use."""


class ProductNotFoundError(InventoryError):
    """Raised when a product ID does not exist."""


class InsufficientStockError(InventoryError):
    """Raised when a sale would reduce the stock below zero."""


class OperationCancelled(Exception):
    """Raised when the user types 'cancel' at a prompt."""


# ---------------------------------------------------------------------------
# Small helper functions
# ---------------------------------------------------------------------------
def _to_whole_number(value: object, label: str) -> int:
    """Convert text or a number to ``int``; reject decimals and junk."""
    message = f"{label} must be a whole number (for example 10)."
    if isinstance(value, bool):
        raise ValidationError(message)
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        raise ValidationError(message) from None
    if not math.isfinite(number) or number != int(number):
        raise ValidationError(message)
    return int(number)


def _fit(text: str, width: int) -> str:
    """Shorten ``text`` with '...' when it is wider than its column."""
    if len(text) <= width:
        return text
    return text[:width - 3] + "..."


def table_header() -> str:
    """Return the table header line followed by an underline."""
    cells = [
        f"{title:{align}{width}}" for title, width, align in TABLE_COLUMNS
    ]
    header = "  ".join(cells)
    return f"{header}\n{'-' * len(header)}"


# ---------------------------------------------------------------------------
# Product hierarchy (encapsulation, inheritance, polymorphism)
# ---------------------------------------------------------------------------
class Product:
    """Base class for every item that can be kept in stock.

    All data lives in protected attributes (leading underscore). Code
    outside the class reads and changes it through properties, so every
    value is validated before it is stored (encapsulation).
    """

    category = "General"

    def __init__(self, product_id: str, name: str, price: float,
                 quantity: int) -> None:
        """Create a product; raise ``ValidationError`` for bad data."""
        self._product_id = self.validate_id(product_id)
        self._name = self.validate_name(name)
        self._price = self.validate_price(price)
        self._quantity = self.validate_quantity(quantity)

    # -- validation rules (also reused by the console prompts) ------------
    @staticmethod
    def validate_id(value: object) -> str:
        """Return the cleaned, upper-case product ID or raise an error."""
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValidationError("Product ID must be letters or digits.")
        text = str(value).strip().upper()
        if not ID_PATTERN.fullmatch(text):
            raise ValidationError(
                f"Product ID must be 1-{MAX_ID_LENGTH} characters and use "
                "only letters, digits, '-' or '_' (no spaces)."
            )
        return text

    @staticmethod
    def validate_name(value: object) -> str:
        """Return the tidied product name or raise ``ValidationError``."""
        if not isinstance(value, str):
            raise ValidationError("Product name must be text.")
        text = " ".join(value.split())
        if not text:
            raise ValidationError("Product name cannot be empty.")
        if len(text) > MAX_NAME_LENGTH:
            raise ValidationError(
                f"Product name can have at most {MAX_NAME_LENGTH} "
                f"characters (you typed {len(text)})."
            )
        return text

    @staticmethod
    def validate_price(value: object) -> float:
        """Return the price rounded to 2 decimals or raise an error."""
        message = "Price must be a number, for example 499.99."
        if isinstance(value, bool):
            raise ValidationError(message)
        try:
            number = float(str(value).replace(",", "").strip())
        except ValueError:
            raise ValidationError(message) from None
        if not math.isfinite(number):
            raise ValidationError(message)
        if number < 0:
            raise ValidationError("Price cannot be negative.")
        price = round(number, 2)
        if price == 0:
            raise ValidationError("Price must be greater than zero.")
        if price > MAX_PRICE:
            raise ValidationError(f"Price cannot exceed {MAX_PRICE:,.2f}.")
        return price

    @staticmethod
    def validate_quantity(value: object) -> int:
        """Return a non-negative whole number or raise an error."""
        number = _to_whole_number(value, "Quantity")
        if number < 0:
            raise ValidationError("Quantity cannot be negative.")
        if number > MAX_QUANTITY:
            raise ValidationError(
                f"Quantity cannot exceed {MAX_QUANTITY:,} units."
            )
        return number

    # -- properties (getters and validated setters) ------------------------
    @property
    def product_id(self) -> str:
        """Unique, upper-case identifier (read-only after creation)."""
        return self._product_id

    @property
    def name(self) -> str:
        """Human-readable product name."""
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = self.validate_name(value)

    @property
    def price(self) -> float:
        """Price of a single unit."""
        return self._price

    @price.setter
    def price(self, value: float) -> None:
        self._price = self.validate_price(value)

    @property
    def quantity(self) -> int:
        """Units currently in stock (use ``adjust_stock`` for changes)."""
        return self._quantity

    @quantity.setter
    def quantity(self, value: int) -> None:
        self._quantity = self.validate_quantity(value)

    # -- stock handling ------------------------------------------------------
    def adjust_stock(self, change: int) -> int:
        """Add (positive) or remove (negative) units; return the new total.

        Raises ``InsufficientStockError`` if the stock would drop below
        zero and ``ValidationError`` for an invalid or oversized change.
        """
        amount = _to_whole_number(change, "Stock change")
        if amount == 0:
            raise ValidationError("Stock change cannot be zero.")
        new_quantity = self._quantity + amount
        if new_quantity < 0:
            raise InsufficientStockError(
                f"Only {self._quantity} unit(s) of '{self._name}' are in "
                f"stock, so {-amount} cannot be removed."
            )
        if new_quantity > MAX_QUANTITY:
            raise ValidationError(
                f"Stock cannot exceed {MAX_QUANTITY:,} units "
                f"(currently {self._quantity})."
            )
        self._quantity = new_quantity
        return new_quantity

    def is_low_stock(self, threshold: int = LOW_STOCK_THRESHOLD) -> bool:
        """Return True when fewer than ``threshold`` units are left."""
        return self._quantity < threshold

    def total_value(self) -> float:
        """Return the value of all units of this product in stock."""
        return self._price * self._quantity

    # -- presentation and storage -------------------------------------------
    def _format_row(self, details: str) -> str:
        """Build one aligned table row from the shared columns + details."""
        cells = (
            self._product_id,
            self.category,
            self._name,
            f"{self._price:,.2f}",
            str(self._quantity),
            details,
        )
        parts = [
            f"{_fit(cell, width):{align}{width}}"
            for cell, (_, width, align) in zip(cells, TABLE_COLUMNS)
        ]
        return "  ".join(parts)

    def display(self) -> None:
        """Print this product as one table row (subclasses override it)."""
        print(self._format_row("-"))

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly dictionary describing this product."""
        return {
            "type": self.category,
            "product_id": self._product_id,
            "name": self._name,
            "price": self._price,
            "quantity": self._quantity,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Product":
        """Rebuild a product from a stored record (subclasses extend it)."""
        return cls(data["product_id"], data["name"], data["price"],
                   data["quantity"])

    def __str__(self) -> str:
        """Return a short label such as ``Laptop (E001)``."""
        return f"{self._name} ({self._product_id})"


class Electronics(Product):
    """An electronic product that comes with a warranty."""

    category = "Electronics"

    def __init__(self, product_id: str, name: str, price: float,
                 quantity: int, warranty_months: int) -> None:
        """Create an electronics product with a warranty in months."""
        super().__init__(product_id, name, price, quantity)
        self._warranty_months = self.validate_warranty(warranty_months)

    @staticmethod
    def validate_warranty(value: object) -> int:
        """Return a valid warranty length in months or raise an error."""
        months = _to_whole_number(value, "Warranty")
        if months < 0:
            raise ValidationError("Warranty cannot be negative.")
        if months > MAX_WARRANTY_MONTHS:
            raise ValidationError(
                f"Warranty cannot exceed {MAX_WARRANTY_MONTHS} months."
            )
        return months

    @property
    def warranty_months(self) -> int:
        """Warranty length in months (0 means no warranty)."""
        return self._warranty_months

    @warranty_months.setter
    def warranty_months(self, value: int) -> None:
        self._warranty_months = self.validate_warranty(value)

    def display(self) -> None:
        """Print the common columns plus the warranty (polymorphism)."""
        print(self._format_row(f"Warranty: {self._warranty_months} mo"))

    def to_dict(self) -> Dict[str, Any]:
        """Extend the base dictionary with the warranty length."""
        data = super().to_dict()
        data["warranty_months"] = self._warranty_months
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Electronics":
        """Rebuild an ``Electronics`` object from a stored record."""
        return cls(data["product_id"], data["name"], data["price"],
                   data["quantity"], data["warranty_months"])


class Groceries(Product):
    """A perishable grocery product with an expiry date."""

    category = "Groceries"

    def __init__(self, product_id: str, name: str, price: float,
                 quantity: int, expiry_date: object) -> None:
        """Create a grocery product; ``expiry_date`` is YYYY-MM-DD."""
        super().__init__(product_id, name, price, quantity)
        self._expiry_date = self.validate_expiry(expiry_date)

    @staticmethod
    def validate_expiry(value: object) -> date:
        """Return a ``date`` from text such as 2026-12-31 or raise."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return datetime.strptime(str(value).strip(), DATE_FORMAT).date()
        except ValueError:
            raise ValidationError(
                "Expiry date must look like YYYY-MM-DD, e.g. 2026-12-31."
            ) from None

    @property
    def expiry_date(self) -> date:
        """The date after which the product must not be sold."""
        return self._expiry_date

    @expiry_date.setter
    def expiry_date(self, value: object) -> None:
        self._expiry_date = self.validate_expiry(value)

    def is_expired(self, today: Optional[date] = None) -> bool:
        """Return True when the expiry date is before ``today``."""
        return self._expiry_date < (today or date.today())

    def display(self) -> None:
        """Print the common columns plus the expiry date (polymorphism)."""
        label = "EXPIRED" if self.is_expired() else "Expires"
        print(self._format_row(f"{label}: {self._expiry_date.isoformat()}"))

    def to_dict(self) -> Dict[str, Any]:
        """Extend the base dictionary with the expiry date."""
        data = super().to_dict()
        data["expiry_date"] = self._expiry_date.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Groceries":
        """Rebuild a ``Groceries`` object from a stored record."""
        return cls(data["product_id"], data["name"], data["price"],
                   data["quantity"], data["expiry_date"])


# Maps the "type" stored in the JSON file to the class that rebuilds it.
PRODUCT_TYPES: Dict[str, Type[Product]] = {
    product_class.category: product_class
    for product_class in (Electronics, Groceries)
}


def product_from_dict(data: object) -> Product:
    """Rebuild the correct ``Product`` subclass from a stored record."""
    if not isinstance(data, dict):
        raise ValidationError("the record is not a JSON object.")
    kind = data.get("type")
    product_class = None
    if isinstance(kind, str):
        product_class = PRODUCT_TYPES.get(kind)
    if product_class is None:
        raise ValidationError(f"unknown product type {kind!r}.")
    try:
        return product_class.from_dict(data)
    except KeyError as error:
        raise ValidationError(f"missing field {error}.") from None


# ---------------------------------------------------------------------------
# Inventory engine (abstraction)
# ---------------------------------------------------------------------------
class Inventory:
    """Core engine that keeps products in a dictionary keyed by ID.

    The dictionary gives fast look-ups by product ID. The class offers
    simple create / read / update / delete operations and hides how the
    data is stored.
    """

    def __init__(
        self, low_stock_threshold: int = LOW_STOCK_THRESHOLD
    ) -> None:
        """Create an empty inventory."""
        self._products: Dict[str, Product] = {}
        self._low_stock_threshold = low_stock_threshold

    @property
    def low_stock_threshold(self) -> int:
        """Products with fewer units than this are 'low on stock'."""
        return self._low_stock_threshold

    def __len__(self) -> int:
        """Return how many different products are stored."""
        return len(self._products)

    def __contains__(self, product_id: object) -> bool:
        """Support ``'E001' in inventory`` (case-insensitive)."""
        return self._normalise(product_id) in self._products

    @staticmethod
    def _normalise(product_id: object) -> str:
        """Clean an ID the same way ``Product`` does."""
        try:
            return Product.validate_id(product_id)
        except ValidationError:
            return str(product_id).strip().upper()

    # -- create / read / update / delete -------------------------------------
    def add_product(self, product: Product) -> None:
        """Register a new product; IDs must be unique."""
        if product.product_id in self._products:
            raise DuplicateProductError(
                f"Product ID '{product.product_id}' already exists."
            )
        self._products[product.product_id] = product

    def get_product(self, product_id: str) -> Product:
        """Return the product with this ID or raise an error."""
        key = self._normalise(product_id)
        try:
            return self._products[key]
        except KeyError:
            raise ProductNotFoundError(
                f"No product found with ID '{key}'. "
                "Use option 2 to see the valid IDs."
            ) from None

    def remove_product(self, product_id: str) -> Product:
        """Delete a product by ID and return the removed object."""
        product = self.get_product(product_id)
        del self._products[product.product_id]
        return product

    def update_stock(self, product_id: str, change: int) -> Product:
        """Add (positive) or remove (negative) stock for one product."""
        product = self.get_product(product_id)
        product.adjust_stock(change)
        return product

    # -- reports ---------------------------------------------------------------
    def get_all_products(self) -> List[Product]:
        """Return every product sorted by ID."""
        return [self._products[key] for key in sorted(self._products)]

    def get_low_stock_products(self) -> List[Product]:
        """Return products below the threshold, emptiest first."""
        low = [
            product for product in self._products.values()
            if product.is_low_stock(self._low_stock_threshold)
        ]
        return sorted(low, key=lambda item: (item.quantity, item.product_id))

    def total_units(self) -> int:
        """Return the number of units in stock across all products."""
        return sum(product.quantity for product in self._products.values())

    def total_value(self) -> float:
        """Return the combined value of everything in stock."""
        return sum(product.total_value()
                   for product in self._products.values())

    # -- helpers ---------------------------------------------------------------
    def generate_product_id(self, prefix: str) -> str:
        """Return the next unused ID such as ``E001`` or ``G003``."""
        number = 1
        while f"{prefix}{number:03d}" in self._products:
            number += 1
        return f"{prefix}{number:03d}"

    def replace_all(self, products: Iterable[Product]) -> None:
        """Replace the whole inventory with the given products."""
        self._products = {product.product_id: product for product in products}


# ---------------------------------------------------------------------------
# File handling (JSON persistence)
# ---------------------------------------------------------------------------
def default_data_path() -> Path:
    """Return the path of ``inventory.json`` next to this script."""
    try:
        folder = Path(__file__).resolve().parent
    except NameError:  # for example inside an interactive shell
        folder = Path.cwd()
    return folder / DATA_FILE_NAME


class LoadResult(NamedTuple):
    """What ``FileHandler.load`` found on disk."""

    products: List[Product]
    warnings: List[str]
    needs_sample_data: bool


class FileHandler:
    """Saves the inventory to a JSON file and loads it back.

    Saving converts every ``Product`` into a dictionary (serialisation).
    Loading rebuilds the right ``Product`` subclass from each dictionary
    (deserialisation).
    """

    def __init__(self, file_path: Optional[Path] = None) -> None:
        """Use ``file_path`` or ``inventory.json`` next to this script."""
        self._path = Path(file_path) if file_path else default_data_path()

    @property
    def path(self) -> Path:
        """Location of the JSON data file."""
        return self._path

    def save(self, inventory: Inventory) -> None:
        """Write every product to the JSON file using a safe, atomic write.

        The data goes to a temporary file first and then replaces the real
        file, so a crash during saving can never leave a half-written file.
        """
        document = {
            "version": 1,
            "last_saved": datetime.now().isoformat(timespec="seconds"),
            "products": [
                product.to_dict() for product in inventory.get_all_products()
            ],
        }
        temp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=4)
                handle.write("\n")
            os.replace(temp_path, self._path)
        except (OSError, TypeError, ValueError):
            with suppress(OSError):
                temp_path.unlink()
            raise

    def load(self) -> LoadResult:
        """Read the JSON file and rebuild the product objects.

        A missing, blank or empty file counts as a first run, so sample
        data is wanted. A damaged file is kept as ``.bak`` so nothing is
        lost, and the program starts with an empty inventory.
        """
        try:
            text = self._path.read_text(encoding="utf-8")
            document = json.loads(text) if text.strip() else None
        except FileNotFoundError:
            return LoadResult([], [], True)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return LoadResult([], [self._set_aside_damaged_file()], False)
        except OSError as error:
            warning = f"Could not read '{self._path.name}': {error}."
            return LoadResult([], [warning], False)

        if document in (None, [], {}):
            return LoadResult([], [], True)
        records = document
        if isinstance(document, dict):
            records = document.get("products")
        if not isinstance(records, list):
            return LoadResult([], [self._set_aside_damaged_file()], False)

        products: List[Product] = []
        warnings: List[str] = []
        seen_ids = set()
        for position, record in enumerate(records, start=1):
            try:
                product = product_from_dict(record)
            except (ValueError, TypeError) as error:
                warnings.append(f"Skipped record #{position}: {error}")
                continue
            if product.product_id in seen_ids:
                warnings.append(
                    f"Skipped record #{position}: duplicate ID "
                    f"'{product.product_id}'."
                )
                continue
            seen_ids.add(product.product_id)
            products.append(product)
        return LoadResult(products, warnings, False)

    def _set_aside_damaged_file(self) -> str:
        """Rename a damaged data file to ``.bak``; return a warning text."""
        backup = self._path.with_name(self._path.name + ".bak")
        try:
            os.replace(self._path, backup)
        except OSError:
            return (f"'{self._path.name}' could not be read. "
                    "Starting with an empty inventory.")
        return (f"'{self._path.name}' was damaged, so it was kept as "
                f"'{backup.name}'. Starting with an empty inventory.")


def create_sample_products() -> List[Product]:
    """Return the starter products used on the very first run."""
    today = date.today()
    return [
        Electronics("E001", "Wireless Mouse", 799, 25, 12),
        Electronics("E002", "Mechanical Keyboard", 3499, 12, 24),
        Electronics("E003", "27-inch Monitor", 14999, 4, 36),
        Electronics("E004", "USB-C Charger 65W", 1799, 40, 18),
        Electronics("E005", "Bluetooth Speaker", 2499, 3, 12),
        Electronics("E006", "Laptop Pro 15", 64999, 7, 24),
        Groceries("G001", "Basmati Rice 5kg", 649, 60,
                  today + timedelta(days=300)),
        Groceries("G002", "Fresh Milk 1L", 62, 18,
                  today + timedelta(days=5)),
        Groceries("G003", "Whole Wheat Bread", 45, 2,
                  today + timedelta(days=3)),
        Groceries("G004", "Olive Oil 500ml", 799, 9,
                  today + timedelta(days=420)),
        Groceries("G005", "Green Tea (25 bags)", 175, 30,
                  today + timedelta(days=240)),
        Groceries("G006", "Greek Yogurt 400g", 120, 14,
                  today - timedelta(days=2)),
    ]


# ---------------------------------------------------------------------------
# Console helpers (every input is validated, so typing can never crash us)
# ---------------------------------------------------------------------------
T = TypeVar("T")
MENU_WIDTH = 64
CANCEL_HINT = "  (type 'cancel' at any prompt to return to the menu)"


def read_line(prompt: str) -> str:
    """Read one stripped line; typing ``cancel`` aborts the operation."""
    text = input(prompt).strip()
    if text.lower() == "cancel":
        raise OperationCancelled()
    return text


def ask(prompt: str, parser: Callable[[str], T],
        default: Optional[str] = None) -> T:
    """Ask until ``parser`` accepts the answer, then return its result.

    Bad input (``ValueError`` or ``IndexError``, including negative or
    non-numeric values) only prints a hint and repeats the question, so
    the program never crashes because of what the user types.
    """
    while True:
        answer = read_line(prompt)
        if not answer and default is not None:
            answer = default
        try:
            return parser(answer)
        except (ValueError, IndexError) as error:
            print(f"  [!] {error}")


def ask_choice(title: str, options: Sequence[str]) -> str:
    """Show a numbered list and return the option the user picks."""
    print(title)
    for number, option in enumerate(options, start=1):
        print(f"   {number}. {option}")
    while True:
        answer = read_line("Enter choice number: ")
        try:
            index = int(answer) - 1
            if index < 0:  # stops "0" and negatives from wrapping around
                raise IndexError(answer)
            return options[index]
        except (ValueError, IndexError):
            print(f"  [!] Please enter a number from 1 to {len(options)}.")


def ask_yes_no(question: str) -> bool:
    """Ask a yes/no question; pressing Enter means 'no'."""
    while True:
        answer = read_line(f"{question} [y/N]: ").lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print("  [!] Please answer with 'y' or 'n'.")


def parse_unit_count(text: str) -> int:
    """Parse a stock change amount; it must be at least one unit."""
    units = Product.validate_quantity(text)
    if units == 0:
        raise ValidationError("Enter at least 1 unit.")
    return units


def print_heading(title: str) -> None:
    """Print a section heading padded with dashes to the menu width."""
    print()
    print(f"--- {title} ".ljust(MENU_WIDTH, "-"))


def print_table(products: Sequence[Product]) -> None:
    """Print the table header followed by one row per product."""
    print(table_header())
    for product in products:
        product.display()  # polymorphism: each class prints its own details


# ---------------------------------------------------------------------------
# Application (menu and user interaction)
# ---------------------------------------------------------------------------
class InventoryApp:
    """Interactive menu that connects the user to the ``Inventory``."""

    def __init__(self, file_handler: Optional[FileHandler] = None) -> None:
        """Create the app; a default ``FileHandler`` is used if omitted."""
        if file_handler is None:
            file_handler = FileHandler()
        self._file_handler = file_handler
        self._inventory = Inventory()
        self._running = True
        self._menu = (
            ("Add Product", self.add_product),
            ("View All Inventory", self.view_inventory),
            ("Update Stock", self.update_stock),
            ("Delete Product", self.delete_product),
            ("Low Stock Alerts", self.show_low_stock),
            ("Save & Exit", self.save_and_exit),
        )

    # -- main loop -------------------------------------------------------------
    def run(self) -> None:
        """Load the data, then show the menu until the user exits."""
        self._start_up()
        while self._running:
            try:
                self._run_once()
            except (EOFError, KeyboardInterrupt):
                print("\n\n  Input closed - saving your data before exit.")
                self._save_and_report()
                self._running = False

    def _start_up(self) -> None:
        """Load saved data, or create sample data on the first run."""
        print()
        print("=" * MENU_WIDTH)
        print("WELCOME".center(MENU_WIDTH))
        print("=" * MENU_WIDTH)
        result = self._file_handler.load()
        for warning in result.warnings:
            print(f"  [!] {warning}")
        if result.needs_sample_data:
            self._inventory.replace_all(create_sample_products())
            print("  First run: sample products were added so you can "
                  "explore the system.")
            self._autosave()
        else:
            self._inventory.replace_all(result.products)
            print(f"  Loaded {len(self._inventory)} product(s) from "
                  f"'{self._file_handler.path.name}'.")

    def _run_once(self) -> None:
        """Show the menu, read one choice and run the chosen action."""
        self._print_menu()
        choice = input(f"Enter your choice (1-{len(self._menu)}): ").strip()
        try:
            index = int(choice) - 1
            if index < 0:  # stops "0" and negatives from wrapping around
                raise IndexError(choice)
            _, handler = self._menu[index]
        except (ValueError, IndexError):
            print(f"\n  [!] '{choice}' is not a valid option. "
                  f"Please choose a number from 1 to {len(self._menu)}.")
            return
        try:
            handler()
        except OperationCancelled:
            print("\n  Cancelled. Nothing was changed.")
        except InventoryError as error:
            print(f"\n  [!] {error}")
        except Exception as error:  # last safety net: never crash the menu
            print(f"\n  [!] Unexpected problem: {error}")

    def _print_menu(self) -> None:
        """Draw the main menu."""
        summary = (f"{len(self._inventory)} product(s)  |  low stock = "
                   f"fewer than {self._inventory.low_stock_threshold} units")
        print()
        print("=" * MENU_WIDTH)
        print("INVENTORY & STOCK MANAGEMENT SYSTEM".center(MENU_WIDTH))
        print(summary.center(MENU_WIDTH))
        print("=" * MENU_WIDTH)
        for number, (label, _) in enumerate(self._menu, start=1):
            print(f"   [{number}]  {label}")
        print("-" * MENU_WIDTH)

    # -- saving helpers ----------------------------------------------------------
    def _autosave(self) -> None:
        """Save quietly after a change so a closed window loses nothing."""
        try:
            self._file_handler.save(self._inventory)
        except (OSError, TypeError, ValueError) as error:
            print(f"  [!] Auto-save failed ({error}). "
                  "Use option 6 to try again.")

    def _save_and_report(self) -> bool:
        """Save the inventory, print the outcome and return success."""
        try:
            self._file_handler.save(self._inventory)
        except (OSError, TypeError, ValueError) as error:
            print(f"  [!] Could not save '{self._file_handler.path}': "
                  f"{error}")
            return False
        print(f"  [OK] {len(self._inventory)} product(s) saved to "
              f"'{self._file_handler.path.name}'.")
        return True

    # -- menu actions ------------------------------------------------------------
    def add_product(self) -> None:
        """Option 1: create an Electronics or Groceries product."""
        print_heading("ADD PRODUCT")
        print(CANCEL_HINT)
        print()
        category = ask_choice(
            "Select the product category:", ("Electronics", "Groceries")
        )
        prefix = "E" if category == "Electronics" else "G"
        product_id = self._ask_new_product_id(
            self._inventory.generate_product_id(prefix)
        )
        name = ask("Product name: ", Product.validate_name)
        price = ask(f"Price per unit ({CURRENCY}): ", Product.validate_price)
        quantity = ask("Starting quantity: ", Product.validate_quantity)
        product: Product
        if category == "Electronics":
            months = ask("Warranty in months (0 = none): ",
                         Electronics.validate_warranty)
            product = Electronics(product_id, name, price, quantity, months)
        else:
            expiry = ask("Expiry date (YYYY-MM-DD): ",
                         Groceries.validate_expiry)
            product = Groceries(product_id, name, price, quantity, expiry)
        self._inventory.add_product(product)
        self._autosave()
        print("\n  [OK] Product added:\n")
        print_table([product])

    def _ask_new_product_id(self, suggested: str) -> str:
        """Ask for an unused product ID; Enter accepts the suggestion."""
        while True:
            product_id = ask(f"Product ID [{suggested}]: ",
                             Product.validate_id, default=suggested)
            if product_id in self._inventory:
                print(f"  [!] ID '{product_id}' is already used. "
                      "Please choose another one.")
                continue
            return product_id

    def view_inventory(self) -> None:
        """Option 2: show every product in an aligned table."""
        print_heading("ALL PRODUCTS")
        products = self._inventory.get_all_products()
        if not products:
            print("  The inventory is empty. Choose option 1 to add items.")
            return
        print()
        print_table(products)
        print(f"\n  Products: {len(products)}    "
              f"Units in stock: {self._inventory.total_units():,}    "
              f"Stock value: {CURRENCY} {self._inventory.total_value():,.2f}")
        low_count = len(self._inventory.get_low_stock_products())
        if low_count:
            print(f"  [!] {low_count} product(s) are low on stock. "
                  "See option 5.")

    def update_stock(self) -> None:
        """Option 3: increase (restock) or decrease (sale) a stock level."""
        print_heading("UPDATE STOCK")
        print(CANCEL_HINT)
        print()
        product = self._inventory.get_product(read_line("Product ID: "))
        print()
        print_table([product])
        print()
        action = ask_choice(
            "What would you like to do?",
            ("Increase stock (restock)", "Decrease stock (sale or damage)"),
        )
        sign = 1 if action.startswith("Increase") else -1
        while True:
            units = ask("Number of units: ", parse_unit_count)
            try:
                self._inventory.update_stock(product.product_id, sign * units)
            except (InsufficientStockError, ValidationError) as error:
                print(f"  [!] {error}")
                continue
            break
        self._autosave()
        verb = "increased" if sign > 0 else "decreased"
        print(f"\n  [OK] Stock {verb} by {units}. "
              f"New quantity of {product}: {product.quantity}.")
        if product.is_low_stock(self._inventory.low_stock_threshold):
            print("  [!] Warning: this product is now low on stock.")

    def delete_product(self) -> None:
        """Option 4: remove a product after asking for confirmation."""
        print_heading("DELETE PRODUCT")
        print(CANCEL_HINT)
        print()
        product = self._inventory.get_product(
            read_line("Product ID to delete: ")
        )
        print()
        print_table([product])
        print()
        if not ask_yes_no(f"Delete {product} permanently?"):
            print("\n  Deletion cancelled. Nothing was changed.")
            return
        self._inventory.remove_product(product.product_id)
        self._autosave()
        print(f"\n  [OK] {product} was deleted.")

    def show_low_stock(self) -> None:
        """Option 5: list products with fewer than 5 units in stock."""
        threshold = self._inventory.low_stock_threshold
        print_heading(f"LOW STOCK ALERTS (fewer than {threshold} units)")
        items = self._inventory.get_low_stock_products()
        if not items:
            print("  Good news: every product has enough stock.")
            return
        print()
        print_table(items)
        empty = sum(1 for item in items if item.quantity == 0)
        print(f"\n  [!] {len(items)} product(s) need restocking "
              f"({empty} completely out of stock).")

    def save_and_exit(self) -> None:
        """Option 6: save everything to the JSON file and quit."""
        print_heading("SAVE & EXIT")
        if not self._save_and_report():
            if not ask_yes_no("  Exit without saving?"):
                return
        print("\n  Thank you for using the Inventory & Stock Management "
              "System. Goodbye!\n")
        self._running = False


def main() -> None:
    """Program entry point."""
    InventoryApp().run()


if __name__ == "__main__":
    main()