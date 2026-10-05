"""Будує RDF-граф рейсів із CSV і зберігає його у Turtle.

Запуск:
    python build_graph.py flights.csv params.json graph.ttl
"""
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

EX = Namespace("http://example.org/mkr/")

# Можливі назви колонок у flights.csv (порівнюються без регістру, пробілів, "_" і "-").
# Якщо у вашому файлі колонка називається інакше — допишіть назву в потрібний список.
COLUMNS = {
    "id": ["flight_id", "id", "flight", "flight_no", "flight_number", "code"],
    "airline": ["airline", "operated_by", "carrier", "company"],
    "from_city": ["from_city", "departure_city", "origin_city", "origin", "from",
                  "departs_from", "dep_city", "city_from", "source_city"],
    "from_country": ["from_country", "departure_country", "origin_country",
                     "dep_country", "country_from", "source_country"],
    "to_city": ["to_city", "arrival_city", "destination_city", "destination", "to",
                "arrives_at", "arr_city", "city_to", "dest_city"],
    "to_country": ["to_country", "arrival_country", "destination_country",
                   "arr_country", "country_to", "dest_country"],
    "date": ["departure_date", "date", "dep_date", "departure"],
    "duration": ["duration_min", "duration", "duration_minutes", "minutes"],
    "price": ["price_eur", "price", "cost_eur", "cost"],
    "note": ["note", "notes", "comment", "remarks"],
}
OPTIONAL = {"price", "note"}
EMPTY = {"", "nan", "none", "null", "n/a", "na", "-"}


# ---------- нормалізація ----------

def clean(value):
    """Обрізає пробіли; порожні значення та заглушки на кшталт NaN перетворює на ''."""
    value = (value or "").strip()
    return "" if value.lower() in EMPTY else value


def iri(kind, name):
    return EX[f"{kind}/{name.replace(' ', '_')}"]


def parse_date(text):
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def parse_int(text):
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        return None


def parse_decimal(text):
    try:
        return Decimal(text.replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None


def parse_note(text):
    """'delay=25;by=AirWatch' -> (25, 'AirWatch'). Якщо затримки немає -> (None, None)."""
    delay = re.search(r"delay\s*=\s*(\d+)", text, re.IGNORECASE)
    if not delay:
        return None, None
    by = re.search(r"by\s*=\s*([^;]+)", text, re.IGNORECASE)
    return int(delay.group(1)), (by.group(1).strip() if by else None)


# ---------- читання CSV ----------

def map_columns(header):
    def norm(name):
        return re.sub(r"[\s_\-]", "", name.lower())

    by_norm = {norm(h): h for h in header}
    mapping = {}
    for field, candidates in COLUMNS.items():
        for cand in candidates:
            if norm(cand) in by_norm:
                mapping[field] = by_norm[norm(cand)]
                break
        else:
            if field not in OPTIONAL:
                sys.exit(f"Не знайдено колонку для '{field}'. Заголовки у файлі: {header}. "
                         f"Допишіть потрібну назву в COLUMNS['{field}'].")
    return mapping


def read_flights(path):
    """Повертає {ID: запис}; дублікати одного рейсу зливаються в один запис."""
    flights = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = map_columns(reader.fieldnames or [])
        for row in reader:
            def get(field):
                return clean(row.get(cols.get(field, ""), ""))

            fid = get("id").upper()
            if not fid:
                continue
            delay, source = parse_note(get("note"))
            record = {
                "airline": get("airline"),
                "from_city": get("from_city").title(),
                "from_country": get("from_country"),
                "to_city": get("to_city").title(),
                "to_country": get("to_country"),
                "date": parse_date(get("date")),
                "duration": parse_int(get("duration")),
                "price": parse_decimal(get("price")),
                "delay": delay,
                "source": source,
            }
            merged = flights.setdefault(fid, record)
            for key, value in record.items():   # дублікат доповнює прогалини першого запису
                if merged[key] in (None, "") and value not in (None, ""):
                    merged[key] = value
    return flights


# ---------- побудова графа ----------

def add_schema(g):
    for cls in ("Flight", "Airline", "City", "Country", "BudgetFlight", "LongFlight"):
        g.add((EX[cls], RDF.type, RDFS.Class))
    g.add((EX.BudgetFlight, RDFS.subClassOf, EX.Flight))
    g.add((EX.LongFlight, RDFS.subClassOf, EX.Flight))

    properties = [
        ("operatedBy", EX.Flight, EX.Airline),
        ("departsFrom", EX.Flight, EX.City),
        ("arrivesAt", EX.Flight, EX.City),
        ("locatedIn", EX.City, EX.Country),
        ("departureDate", EX.Flight, XSD.date),
        ("durationMin", EX.Flight, XSD.integer),
        ("priceEur", EX.Flight, XSD.decimal),
        ("hasDelayMinutes", EX.Flight, XSD.integer),
        ("reportedBy", RDF.Statement, RDFS.Resource),
        ("fromBusyCountry", EX.Flight, XSD.boolean),
    ]
    for name, domain, range_ in properties:
        g.add((EX[name], RDF.type, RDF.Property))
        g.add((EX[name], RDFS.domain, domain))
        g.add((EX[name], RDFS.range, range_))


def add_named(g, kind, cls, name):
    """Створює вузол міста / країни / авіакомпанії з типом і міткою."""
    node = iri(kind, name)
    g.add((node, RDF.type, cls))
    g.add((node, RDFS.label, Literal(name, lang="en")))
    return node


def add_city(g, city, country):
    node = add_named(g, "city", EX.City, city)
    if country:
        g.add((node, EX.locatedIn, add_named(g, "country", EX.Country, country)))
    return node


def build_graph(flights, params):
    budget = Decimal(str(params["budget_threshold_eur"]))
    long_min = int(params["long_flight_min"])

    g = Graph()
    g.bind("ex", EX)
    add_schema(g)

    # скільки рейсів вилітає з кожної країни (рахуємо вже після злиття дублікатів)
    departures = Counter(f["from_country"] for f in flights.values() if f["from_country"])

    for fid, f in flights.items():
        flight = iri("flight", fid)

        # тип рейсу
        is_budget = f["price"] is not None and f["price"] <= budget
        is_long = f["duration"] is not None and f["duration"] >= long_min
        if is_budget:
            g.add((flight, RDF.type, EX.BudgetFlight))
        if is_long:
            g.add((flight, RDF.type, EX.LongFlight))
        if not (is_budget or is_long):
            g.add((flight, RDF.type, EX.Flight))

        # зв'язки з іншими вузлами
        if f["airline"]:
            g.add((flight, EX.operatedBy, add_named(g, "airline", EX.Airline, f["airline"])))
        if f["from_city"]:
            g.add((flight, EX.departsFrom, add_city(g, f["from_city"], f["from_country"])))
        if f["to_city"]:
            g.add((flight, EX.arrivesAt, add_city(g, f["to_city"], f["to_country"])))

        # літерали
        if f["date"] is not None:
            g.add((flight, EX.departureDate, Literal(f["date"].isoformat(), datatype=XSD.date)))
        if f["duration"] is not None:
            g.add((flight, EX.durationMin, Literal(f["duration"], datatype=XSD.integer)))
        if f["price"] is not None:          # немає ціни -> немає тріпла
            g.add((flight, EX.priceEur, Literal(f["price"], datatype=XSD.decimal)))

        # затримка — через реїфікацію, а не напряму до рейсу
        if f["delay"] is not None:
            stmt = URIRef(EX[f"stmt/{fid.replace(' ', '_')}-delay"])
            g.add((stmt, RDF.type, RDF.Statement))
            g.add((stmt, RDF.subject, flight))
            g.add((stmt, RDF.predicate, EX.hasDelayMinutes))
            g.add((stmt, RDF.object, Literal(f["delay"], datatype=XSD.integer)))
            if f["source"]:
                g.add((stmt, EX.reportedBy, iri("source", f["source"])))

        # країни з частими вильотами
        if departures[f["from_country"]] >= 2:
            g.add((flight, EX.fromBusyCountry, Literal(True)))

    return g


def main():
    if len(sys.argv) != 4:
        sys.exit("Використання: python build_graph.py flights.csv params.json graph.ttl")
    csv_path, params_path, out_path = sys.argv[1:]

    with open(params_path, encoding="utf-8") as f:
        params = json.load(f)

    flights = read_flights(csv_path)
    graph = build_graph(flights, params)
    graph.serialize(destination=out_path, format="turtle")
    print(f"Рейсів: {len(flights)}, тріплів: {len(graph)} -> {out_path}")


if __name__ == "__main__":
    main()