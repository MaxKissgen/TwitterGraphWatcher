import time
import pandas

from SPARQLWrapper import SPARQLWrapper, JSON
import tweepy

# Parliaments: Swiss/Q18510612 French/Q3044918
# French2022: Add to ?statement pq:P2937 wd:Q112567597.
# French2017: Add to ?statement pq:P2937 wd:Q24939798
# German: Add to ?statement ps:P39 wd:Q1939555, pq:P2937 wd:Q33091469
# Austrian: Add to ?statement ps:P39 wd:Q17535155, pq:P2937 wd:Q69340785
# Czech: Add to ?statement ps:P39 wd:Q19803234, pq:P2937 wd:Q108870117
# Danish Old: Add to ?statement ps:P39 wd:Q12311817, pq:P2937 wd:Q63487103
# Danish: Add to ?statement ps:P39 wd:Q12311817, pq:P2937 wd:Q114902058
# Italian_Old: Add to ?statement ps:P39 wd:Q18558478, pq:P2937 wd:Q48799610
# Italian: Add to ?statement ps:P39 wd:Q18558478, pq:P2937 wd:Q114381503 STILL NEED TO DO IT, ATM NOT MANY REGISTERED ON WIKIDATA
# British: Add to ?statement ps:P39 wd:Q77685926, pq:P2937 wd:Q77685395
# Spanish: Add to ?statement ps:P39 wd:Q18171345, pq:P2937 wd:Q77871368
def collect_parliaments_from_wikidata():
    sparql = SPARQLWrapper('https://query.wikidata.org/bigdata/namespace/wdq/sparql')
    sparql.setQuery('''SELECT DISTINCT ?item ?itemLabel (GROUP_CONCAT(?image) as ?images) (GROUP_CONCAT(?firstname) as ?firstnames) (GROUP_CONCAT(?lastname) as ?lastnames) ?fullname (GROUP_CONCAT(?starttime) as ?starttimes) WHERE { 
                              ?item wdt:P31 wd:Q5; 
                                     #wd:Q1766887; 
                                     #wd:Q3603136; 
                                    p:P39 ?statement. 
                              OPTIONAL {?item wdt:P18 ?image. } 
                              OPTIONAL {?item wdt:P106 wd:Q82955.}
                              OPTIONAL {
                                ?item wdt:P1559 ?fullname. 
                              } 
                              OPTIONAL {
                                ?item wdt:P735 ?firstnameL.
                                ?firstnameL wdt:P1705 ?firstname. } 
                              OPTIONAL {
                                ?item wdt:P734 ?lastnameL.
                                ?lastnameL wdt:P1705 ?lastname. 
                              } 

                              OPTIONAL { ?statement pq:P580 ?starttime. } 
                              ?statement ps:P39 wd:Q18171345; 
                                         pq:P2937 wd:Q77871368. 
                              OPTIONAL { 
                                ?statement ps:P39 wd:Q18171345; 
                                           pq:P2937 wd:Q77871368;
                                           pq:P582 ?endtime. }   
                              FILTER(!bound(?endtime) || ?endtime > "2022-02-22")  

                              FILTER NOT EXISTS {
                                ?item wdt:P570 ?deathdate. 
                                FILTER(?deathdate < "2022-02-22")
                              } 
                              SERVICE wikibase:label {
                                bd:serviceParam wikibase:language "en" .
                                ?item rdfs:label ?itemLabel
                              }
                            }
                            GROUP BY ?item ?itemLabel ?fullname 
                            ORDER BY DESC(?item) 
                            LIMIT 1000''')

    sparql.setReturnFormat(JSON)
    try:
        ret = sparql.queryAndConvert()
        f = open("spanishParliament.csv", "w", encoding="utf-8")
        f.writelines("name;image;link;firstname;lastname;politicianstart\n")
        f = open("spanishParliament.csv", "a", encoding="utf-8")
        for r in ret["results"]["bindings"]:
            print(r)
            f.write(
                r["itemLabel"]["value"] + ";" + r["images"]["value"] + ";" + r["item"]["value"] + ";" + r["firstnames"][
                    "value"] + ";" + r["lastnames"]["value"] + ";" + r["starttimes"]["value"] + "\n")
    except Exception as e:
        print("MEEM:" + e)


def collectParliamentAccountsByWikidata(api, wikidata_csv_path):
    parliament = pandas.read_csv(wikidata_csv_path, sep=";", encoding="latin")
    parliament = pandas.DataFrame(parliament, columns=["name", "link"])

    print(parliament)
    f = open("parliament.csv", "w", encoding="utf-8")
    f.writelines("name;id;image;searched on twitter;twitter image;twitter;twitter description\n")
    for ind in range(0, len(parliament.index)):
        print(parliament["name"][ind],
              parliament["link"][ind].removeprefix("http://www.wikidata.org/entity/").removeprefix(
                  "https://www.wikidata.org/wiki/"))

        sparql = SPARQLWrapper('https://query.wikidata.org/bigdata/namespace/wdq/sparql')
        sparql.setQuery(
            "SELECT DISTINCT (GROUP_CONCAT(?image) as ?images) (GROUP_CONCAT(?twitter) as ?twitters) WHERE { "
            + "OPTIONAL { wd:" + parliament["link"][ind].removeprefix("http://www.wikidata.org/entity/").removeprefix(
                "https://www.wikidata.org/wiki/") + " wdt:P18 ?image. }"
            + "OPTIONAL { wd:" + parliament["link"][ind].removeprefix("http://www.wikidata.org/entity/").removeprefix(
                "https://www.wikidata.org/wiki/") + " wdt:P2002 ?twitter. }"
            + "} LIMIT 1")

        sparql.setReturnFormat(JSON)
        try:
            ret = sparql.queryAndConvert()
            f = open("parliament.csv", "a", encoding="utf-8")
            r = ret["results"]["bindings"][0]

            twitter_handle = r["twitters"]["value"]
            twitter_image_url = ""
            twitter_description = ""
            searched_on_twitter = False

            if r["twitters"]["value"] == "":
                print("No twitter handle known, searching...")
                search_results = api.search_users(parliament["name"][ind], page=1, count=1, include_entities=False)
                if len(search_results) != 0:
                    twitter_handle = search_results[0]._json["screen_name"]
                    twitter_image_url = search_results[0]._json["profile_image_url_https"]
                    twitter_description = search_results[0]._json["description"]
                else:
                    print("no handle found")
                searched_on_twitter = True

            f.write(parliament["name"][ind] + ";" + parliament["link"][ind].removeprefix(
                "http://www.wikidata.org/entity/").removeprefix("https://www.wikidata.org/wiki/")
                    + ";" + r["images"]["value"] + ";" + str(
                searched_on_twitter) + ";" + twitter_image_url + ";" + twitter_handle + ";" + twitter_description + "\n")

        except Exception as e:
            print("GOT ERROR")
            print(e)
            ind -= 1
            time.sleep(60)

        time.sleep(.1)

    # response = api.search_users(q="Omid Nouripour", count=1, include_entities=False)
    # print(response)

    # f = open("requestUsersOutput.json", "a", encoding="utf-8")
    # for user in response:
    #    f.write(json.dumps(user._json) + "\n")

