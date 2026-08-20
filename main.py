# Import the pipelines from the newly created pipelines.py file
from src.pipelines import run_license_pipeline, run_author_pipeline, NAME_REPAIR_TASKS
from src.functions_license import count_elastic_documents
from src.functions_name import count_elastic_authors

# Above this many records, "all" has to be confirmed: the scraping and the ORCID
# lookups run at roughly a record per second and the run cannot be resumed.
CONFIRM_ABOVE = 5000

def ask_source(what="documents"):
    """Where the records come from. Elasticsearch needs ES_HOST / ES_USER /
    ES_PASS in .env; the GoTriple API needs nothing."""
    print(f"\nWhere should the {what} come from?")
    print("  1. GoTriple API   (public, no credentials, one page per run)")
    print("  2. Elasticsearch  (direct index access, pages past 10,000 results)")

    answer = input("\nEnter your choice (1 or 2) [default 1]: ").strip()
    return "elastic" if answer == '2' else "gotriple"

def ask_amount(what="records", counter=None):
    """How many records to process. Enter on its own means every match.

    `counter` is an optional callable returning the size of the full population,
    so "all" can be confirmed before a run that would take days.
    """
    answer = input(f"\nHow many {what} should be processed? [Enter = all]: ").strip()

    if answer:
        try:
            return str(int(answer))
        except ValueError:
            print(f"'{answer}' is not a number - processing all of them.")

    if counter is not None:
        try:
            total = counter()
        except Exception as error:
            print(f"(could not count them first: {str(error)[:80]})")
            return "all"

        print(f"-> that is {total:,} records.")
        if total > CONFIRM_ABOVE:
            print(f"   At roughly a second each this run would take about "
                  f"{total / 3600:,.0f} hours and cannot be resumed.")
            if input("   Type 'yes' to go ahead, anything else to cancel: ").strip().lower() != "yes":
                return None
    return "all"


def ask_author_task():
    """Every author job the pipeline can do, as one flat list.

    Returns (task, strategy). Strategy is only meaningful for disambiguation.
    """
    print("\nWhich author problem do you want to solve?")
    print("  NAME RECOVERY")
    print("    1. Names with an ORCID inside     (almost all recoverable)")
    print("    2. Empty names                    (rarely recoverable)")
    print("    3. URL or bare-digit names        (rarely recoverable)")
    print("    4. All broken names at once       ")
    print("  DISAMBIGUATION")
    print("    5. Authors matching a name you type")
    print("    6. The most duplicated name        (picks it automatically)")

    answer = input("\nEnter your choice (1-6) [default 1]: ").strip() or '1'

    tasks = {
        '1': "orcid_names",
        '2': "empty_names",
        '3': "junk_names",
        '4': "broken_names",
        '6': "top_author",
    }
    if answer in tasks:
        return tasks[answer], "cluster"

    if answer != '5':
        print(f"'{answer}' is not one of the options - recovering ORCID-bearing names.")
        return "orcid_names", "cluster"

    print("\nHow should they be disambiguated?")
    print("  1. Clustering     (deterministic, transitive, same answer every run)")
    print("  2. Heuristic      (original registry: first-seen-wins, order-dependent)")

    strategy = "heuristic" if input("\nEnter your choice (1 or 2) [default 1]: ").strip() == '2' else "cluster"
    return "disambiguate", strategy


def start_license_pipeline():
    """Ask how the license run should be scoped, then run it. False = cancelled."""
    source = ask_source("documents")
    counter = (lambda: count_elastic_documents("unresolved")) if source == "elastic" else None
    amount = ask_amount("documents", counter)
    if amount is None:
        print("Cancelled.")
        return False
    run_license_pipeline(num_docs=amount, source=source)
    return True


def ask_query(task, default=None):
    """The name to search for. What Enter means depends on the task."""
    if task in NAME_REPAIR_TASKS:
        return None  # these select on the state of the name, not on a name

    if task == "top_author":
        prompt = "\nWhich name? [Enter = the most duplicated one]: "
    else:
        hint = f"Enter = {default!r}" if default else "Enter = every profile"
        prompt = f"\nWhich author name should be searched? [{hint}]: "

    answer = input(prompt).strip()
    if answer:
        return answer
    return None if task == "top_author" else default


def start_author_pipeline(api_author_params):
    """Ask what the author run should do and how much of it. False = cancelled."""
    task, strategy = ask_author_task()
    source = ("elastic" if task in NAME_REPAIR_TASKS or task == "top_author"
              else ask_source("author profiles"))

    query = ask_query(task, api_author_params.get("q"))
    if task == "top_author" and query:
        print(f"-> disambiguating {query!r} instead of the most duplicated name")

    counter = None
    if source == "elastic":
        if task in NAME_REPAIR_TASKS:
            scope = NAME_REPAIR_TASKS[task][0]
            counter = lambda: count_elastic_authors(name_filter=scope)
        elif task == "top_author" and query:
            counter = lambda: count_elastic_authors(exact_name=query)
        elif task == "disambiguate":
            counter = lambda: count_elastic_authors(query=query)

    amount = ask_amount("author profiles", counter)
    if amount is None:
        print("Cancelled.")
        return False

    run_author_pipeline(api_params={**api_author_params, "size": amount, "q": query},
                        source=source, task=task, strategy=strategy or "cluster")
    return True


def main():
    print("==========================================")
    print("  GOTRIPLE DATA  CLEANING SCRIPT          ")
    print("==========================================")
    print("Please select which pipeline you want to run:")
    print("1. Document License Pipeline")
    print("2. Author Name & ORCID Pipeline")
    print("3. Run Both Pipelines")
    
    choice = input("\nEnter your choice (1, 2, or 3): ").strip()

    api_author_params ={"q": "Wang Y.", "page": 1, "size": 1000, "sort": "name:desc"}

    if choice == '1':
        if not start_license_pipeline():
            return
    elif choice == '2':
        if not start_author_pipeline(api_author_params):
            return
    elif choice == '3':
        if not start_license_pipeline():
            return
        if not start_author_pipeline(api_author_params):
            return
    else:
        print("Invalid choice. Exiting script.")
        return
        
    print("\n==========================================")
    print("EXECUTION COMPLETED!")
    print("==========================================")

if __name__ == "__main__":
    main()