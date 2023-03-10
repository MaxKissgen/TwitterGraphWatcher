import threading
from datetime import datetime, timezone, timedelta
from dateutil.relativedelta import *
import json
import re
import subprocess
import sys

from configobj import ConfigObj

import time
import pandas as pd

import os
import socket
from pyArango.connection import *
import pyArango.theExceptions as pyArangoExceptions
import tweepy
from requests import ReadTimeout
from urllib3.exceptions import ReadTimeoutError
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import config
import politician_collection

#TODO: Integrate date to do catch up from
catch_up_date = datetime(2022, 10, 16)

db_connection = None
db_process = None

current_person = None
collecting_people = False
current_start_date = None
current_end_date = None

people = pd.DataFrame()


class SavePoint:
    person = None
    tweets_left = None
    pagination_token = None
    query = None
    like_pagination = None
    current_start_date = None


savepoint = SavePoint()

translated_wikidata_ids = dict()

exit_sleep = threading.Event()


#TODO: Check this, sometimes it doesnt seem to work
def sleep_interruptible(time_in_secs):
    print("Rate limit hit, sleeping " + str(time_in_secs) + " seconds")
    while not exit_sleep.is_set():
        exit_sleep.wait(time_in_secs)
    exit_sleep.clear()


# TODO: Change test length depending on academic access or not
def query_short_enough(query):
    if len(query.encode("utf-8")) + 5 + 15 > 1024:  # extra length that would come from the "from:username" part where the username can be 15 chars max
        return False
    return True


# TODO: Get actual length of emojis, currently its a bit hacky and just assumes maximal(hopefully?) possible size
def build_query_disjunction(query, query_tail, elements):
    need_another_query = False
    changed_elements = []
    for element in elements:
        if not query_short_enough(query + element + " OR " + query_tail):
            if query_short_enough(query + element + query_tail):
                query += element
            changed_elements = elements[elements.index(element):]  # remove all elements until current one
            need_another_query = True
            break
        query += element + " OR "
    return query, need_another_query, changed_elements


def build_queries(filter_emojis=[], filter_keywords=[], filter_hashtags=[], filter_handles=[]):
    built_queries = [""]
    queries_done = False
    query_tail = ")"
    i = 0

    # If we don't use any filters at all
    if (len(filter_emojis) == 0 or (len(filter_emojis) == 1 and filter_emojis[0] == 0))\
            and (len(filter_keywords) == 0 or (len(filter_keywords) == 1 and filter_keywords[0] == 0)) \
            and (len(filter_hashtags) == 0 or (len(filter_hashtags) == 1 and filter_hashtags[0] == 0)) \
            and (len(filter_handles) == 0 or (len(filter_emojis) == 1 and filter_handles[0] == 0)):
        return built_queries

    #TODO: Check emoji and word syntax, try to understand how to check for non-english letters etc

    while not queries_done:
        need_another_query = False

        #if user_handle.startswith("@"):
        #    user_handle = user_handle.lstrip(user_handle[0])

        #if len(user_handle) > 0:
        built_queries[i] = "("


        if len(filter_emojis) > 0 and filter_emojis[0] != "" and not need_another_query:
            built_queries[i], need_another_query, filter_emojis = build_query_disjunction(built_queries[i], query_tail,
                                                                                          filter_emojis)
        if len(filter_keywords) > 0 and filter_keywords[0] != "" and not need_another_query:
            built_queries[i], need_another_query, filter_keywords = build_query_disjunction(built_queries[i], query_tail,
                                                                                            filter_keywords)
        if len(filter_hashtags) > 0 and filter_hashtags[0] != "" and not need_another_query:
            built_queries[i], need_another_query, filter_hashtags = build_query_disjunction(built_queries[i], query_tail,
                                                                                            filter_hashtags)
        if len(filter_handles) > 0 and filter_handles[0] != "" and not need_another_query:
            built_queries[i], need_another_query, filter_handles = build_query_disjunction(built_queries[i], query_tail,
                                                                                           filter_handles)

        #TODO: This here is probably unnecessary, so likely remove first if
        if built_queries[i].endswith(" OR "):
            built_queries[i] = built_queries[i][:-4]
        elif built_queries[i].endswith(" OR )"):
            built_queries[i] = built_queries[i][:-5] + ")"
        built_queries[i] += query_tail
        if need_another_query:
            built_queries.append("")
            i += 1
        else:
            queries_done = True

    if built_queries[0] == "()":
        built_queries[0] = ""
    return built_queries


def setup_database():
    global db_connection
    db_config = ConfigObj("db_config.properties")

    if len(db_config) == 0:
        raise ValueError("database config empty/not found")

    print("USING TEST PORT")
    db_config['database_port'] = "8530"  # TODO: REMOVE THIS PORT, THIS IS FOR TESTING

    arango_url = db_config['database_connection_type'] + "://" + db_config['database_address'] + ":" + db_config['database_port']
    username = db_config['username']
    password = db_config['password']

    db_running = False
    try:  # Check if something is already bound to the db port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((db_config['database_address'], int(db_config['database_port'])))
        s.close()
    except OSError:  # If yes, check if its arangodb
        print("Something already running on database port, checking if its the database...")
        try:
            db_connection = Connection(username=username, password=password, arangoURL=arango_url)
            print(db_connection.getDatabasesURL())
            db_running = True
        except ConnectionError:
            print("ConnectionError: Could not connect to database. Check that nothing else is running on " + arango_url + " and that this is the right address")

    if not db_running: # Else start the db ourselves
        arango_filepath = os.path.dirname(os.path.abspath(__file__))
        if 'linux' in sys.platform:
            arango_filepath += "/DB/usr/bin/arangod"
        if 'win32' in sys.platform:
            arango_filepath += "\\DB\\usr\\bin\\arangod.exe"
        print("PATH IS: " + arango_filepath)
        print("Database was not running. Starting it...")
        global db_process
        #TODO: REMOVE CUSTOM DIRECTORY
        db_process = subprocess.Popen([arango_filepath, "--database.directory", "TestDBFiles", "--http.trusted-origin", "*", "--server.endpoint", db_config['database_connection_type'] + "+tcp://" + db_config['database_address'] + ":" + db_config['database_port']])

    db_running = False
    while not db_running:  # Wait for the db to start
        try:
            db_connection = Connection(username=username, password=password, arangoURL=arango_url)
            print("Database is running")
            db_running = True
        except ConnectionError as dbex:
            if json.loads(dbex.errors)["code"] == 503:
                time.sleep(1)
            else:
                raise dbex

    # Create TwitterWatcher database and add collections
    if not db_connection.hasDatabase("TwitterWatcher"):
        db_connection.createDatabase("TwitterWatcher")

    database = db_connection["TwitterWatcher"]
    if not db_connection["TwitterWatcher"].hasCollection("People"):
        database.createCollection(className="Collection", name="People", allowUserKeys=True)
    if not db_connection["TwitterWatcher"].hasCollection("Tweets"):
        database.createCollection(className="Collection", name="Tweets", allowUserKeys=True)
    if not db_connection["TwitterWatcher"].hasCollection("Retweets"):
        database.createCollection(className="Edges", name="Retweets", allowUserKeys=True)
    if not db_connection["TwitterWatcher"].hasCollection("QuoteTweets"):
        database.createCollection(className="Edges", name="QuoteTweets", allowUserKeys=True)
    if not db_connection["TwitterWatcher"].hasCollection("Replies"):
        database.createCollection(className="Edges", name="Replies", allowUserKeys=True)
    if not db_connection["TwitterWatcher"].hasCollection("Mentions"):
        database.createCollection(className="Edges", name="Mentions", allowUserKeys=True)
    if not db_connection["TwitterWatcher"].hasCollection("Likes"):
        database.createCollection(className="Edges", name="Likes", allowUserKeys=True)
    if not db_connection["TwitterWatcher"].hasCollection("Follows"):  # Check if I'm actually doing this
        database.createCollection(className="Edges", name="Follows", allowUserKeys=True)
    if not db_connection["TwitterWatcher"].hasCollection("UserBotDetectionValues"):  # Check if I'm actually doing this
        database.createCollection(className="Collection", name="UserBotDetectionValues", allowUserKeys=True)

# Stops the database (if it was started by the watcher)
def stop_database():
    if db_process is not None:
        print("Shutting down started database...")
        subprocess.Popen.kill(db_process)

#TODO: botness average
#def create_person_document(twitter_data,wikidata_data):


def collect_ids_from_wikidata_claims(claims):
    global translated_wikidata_ids
    ids_to_be_translated = dict(translated_wikidata_ids)
    for w_property in claims.keys():
        if ids_to_be_translated.get(w_property) is None:
            ids_to_be_translated[w_property] = ""  # If it is not None, then we already know that key
        for property_value in claims[w_property]:
            if property_value["mainsnak"].get("datavalue") is not None:  # May have unknown value, then we ignore
                if isinstance(property_value["mainsnak"]["datavalue"]["value"], dict) and \
                        property_value["mainsnak"]["datavalue"]["value"].get("id") is not None:
                    if ids_to_be_translated.get(property_value["mainsnak"]["datavalue"]["value"]["id"]) is None:
                        ids_to_be_translated[property_value["mainsnak"]["datavalue"]["value"]["id"]] = ""
                if property_value.get("qualifiers") is not None:
                    for qualifier in property_value["qualifiers"].keys():
                        # print(" ", qualifier)
                        if ids_to_be_translated.get(qualifier) is None:
                            ids_to_be_translated[qualifier] = ""
                        for qualifier_value in property_value["qualifiers"][qualifier]:
                            if qualifier_value.get("datavalue") is not None:  # May have unknown value, then we ignore
                                if isinstance(qualifier_value["datavalue"]["value"], dict) and qualifier_value["datavalue"]["value"].get("id") is not None:
                                    if ids_to_be_translated.get(qualifier_value["datavalue"]["value"]["id"]) is None:
                                        ids_to_be_translated[qualifier_value["datavalue"]["value"]["id"]] = ""

    #print(str(list(ids_to_be_translated.keys())).replace("'", "").replace("'", "").replace(", ", "|")[1:-1], "\n")

    smaller_list = []
    i = 0
    for item in ids_to_be_translated.keys():
        if i == 0:
            smaller_list = []
        if ids_to_be_translated[item] == "":  # Only translate those that need to be translated
            smaller_list.append(item)
        i += 1
        if i == 50 or (len(ids_to_be_translated) != 0 and item == list(ids_to_be_translated.keys())[-1]):
            i = 0
            # Get id translations, this needs to be changed for more languages if we expand to more countries
            if len(smaller_list) != 0:  # Happens when we already translated many ids
                request_successful = False
                while not request_successful:
                    response = requests.get("https://www.wikidata.org/w/api.php?action=wbgetentities&ids="
                                            + str(smaller_list).replace("'", "").replace("'", "").replace(", ", "|")[
                                              1:-1] + "&format=json&props=labels&languages=en|gd|de|es|ca|it|fr|da|cs")
                    time.sleep(0.1)
                    if response.status_code == 200:
                        request_successful = True
                    elif response.status_code == 429:
                        sleep_interruptible(60)
                    else:
                        print("Wikidata id translation request not successful: " + str(response))
                        return ids_to_be_translated
                # print(response.content)

                content_json = json.loads(response.content)
                for w_property in content_json["entities"].keys():
                    # print(content_json["entities"][key], ":", content_json["entities"][key]["labels"])
                    if content_json["entities"][w_property].get("labels") is None:
                        ids_to_be_translated[w_property] = ids_to_be_translated[w_property]
                    elif content_json["entities"][w_property]["labels"].get("en") is not None:
                        ids_to_be_translated[w_property] = content_json["entities"][w_property]["labels"]["en"]["value"]
                    elif len(content_json["entities"][w_property]["labels"]) != 0:
                        # print("got non eng")
                        ids_to_be_translated[w_property] = list(content_json["entities"][w_property]["labels"].values())[0]["value"]

    return ids_to_be_translated


#TODO: Case for just name
def collect_person_from_wikidata(label=None, wikidata_id=None):
    global translated_wikidata_ids

    if wikidata_id is not None:
        #entity_dict = get_entity_dict_from_api(wikidata_id) #TODO: Replace this library to catch a proper requests error.

        response = requests.get("https://www.wikidata.org/wiki/Special:EntityData/" + wikidata_id + ".json?flavor=dumps")
        if response.status_code == 400:
            raise FileNotFoundError("Wikidata")
        entity_dict = json.loads(response.content)["entities"][list(json.loads(response.content)["entities"].keys())[0]]

        claims = dict(entity_dict["claims"])

        # Remove external id claims as we don't need them for our database
        for key in entity_dict["claims"].keys():
            # print(key)
            if claims[key][0]["mainsnak"]["datatype"] == "external-id":
                claims.pop(key)

        translated_wikidata_ids = collect_ids_from_wikidata_claims(claims)

        #print(translated_wikidata_ids)

        transformed_data = dict()


        transformed_data["id"] = entity_dict["id"]

        if entity_dict["labels"].get("en") is not None:
            transformed_data["name"] = entity_dict["labels"]["en"]["value"]
        else:
            transformed_data["name"] = entity_dict["labels"][list(entity_dict["labels"].keys())[0]]["value"]

        if entity_dict["descriptions"].get("en") is not None:
            transformed_data["description"] = entity_dict["descriptions"]["en"]["value"]
        elif len(entity_dict["descriptions"]) != 0:
            transformed_data["description"] = entity_dict["descriptions"][list(entity_dict["descriptions"].keys())[0]]["value"]

        if entity_dict["aliases"].get("en") is not None:
            aliases_slim = []
            for alias in entity_dict["aliases"]["en"]:
                aliases_slim.append(alias["value"])
            transformed_data["aliases"] = aliases_slim
        elif len(entity_dict["aliases"]) != 0:
            aliases_slim = []
            for alias in entity_dict["aliases"][list(entity_dict["aliases"].keys())[0]]:
                aliases_slim.append(alias["value"])
            transformed_data["aliases"] = aliases_slim

        for w_property in claims.keys():
            transformed_data[translated_wikidata_ids[w_property]] = []
            for property_value in claims[w_property]:
                if property_value["mainsnak"].get("datavalue") is not None:  # May have unknown value, then we ignore
                    if not isinstance(property_value["mainsnak"]["datavalue"]["value"], dict):
                        transformed_data[translated_wikidata_ids[w_property]].append(dict({"value": property_value["mainsnak"]["datavalue"]["value"]}))
                    elif property_value["mainsnak"]["datavalue"]["type"] == "time":
                        transformed_data[translated_wikidata_ids[w_property]].append(dict({"value": property_value["mainsnak"]["datavalue"]["value"]["time"].removeprefix("+")}))
                    elif property_value["mainsnak"]["datavalue"]["type"] == "quantity":
                        transformed_data[translated_wikidata_ids[w_property]].append(dict({"value": property_value["mainsnak"]["datavalue"]["value"]["amount"].removeprefix("+")}))
                    elif property_value["mainsnak"]["datavalue"]["value"].get("text") is not None:
                        transformed_data[translated_wikidata_ids[w_property]].append(dict({"value": property_value["mainsnak"]["datavalue"]["value"]["text"]}))
                    elif property_value["mainsnak"]["datavalue"]["value"].get("id") is not None:
                        transformed_data[translated_wikidata_ids[w_property]].append(dict({"value": translated_wikidata_ids[property_value["mainsnak"]["datavalue"]["value"]["id"]]}))
                    else:
                        print("UNKNOWN BEHAVIOUR FOR PROPERTY VALUE: " + w_property + "; " + str(property_value["mainsnak"]["datavalue"]["value"]) + ", getting first element of value dict")
                        transformed_data[translated_wikidata_ids[w_property]].append(dict({"value": list(property_value["mainsnak"]["datavalue"]["value"].values())[0]}))

                    if property_value.get("qualifiers") is not None:
                        transformed_qualifiers = {}
                        for w_qualifier in property_value["qualifiers"].keys():
                            transformed_qualifiers[translated_wikidata_ids[w_qualifier]] = []
                            for qualifier_value in property_value["qualifiers"][w_qualifier]:
                                if qualifier_value.get("datavalue") is not None:  # May have unknown value, then we ignore
                                    if not isinstance(qualifier_value["datavalue"]["value"], dict):
                                        transformed_qualifiers[translated_wikidata_ids[w_qualifier]].append(qualifier_value["datavalue"]["value"])
                                    elif qualifier_value["datavalue"]["type"] == "time":
                                        transformed_qualifiers[translated_wikidata_ids[w_qualifier]].append(qualifier_value["datavalue"]["value"]["time"].removeprefix("+"))
                                    elif qualifier_value["datavalue"]["type"] == "quantity":
                                        transformed_qualifiers[translated_wikidata_ids[w_qualifier]].append(qualifier_value["datavalue"]["value"]["amount"].removeprefix("+"))
                                    elif qualifier_value["datavalue"]["value"].get("text") is not None:
                                        transformed_qualifiers[translated_wikidata_ids[w_qualifier]].append(qualifier_value["datavalue"]["value"]["text"])
                                    elif qualifier_value["datavalue"]["value"].get("id") is not None:
                                        transformed_qualifiers[translated_wikidata_ids[w_qualifier]].append(translated_wikidata_ids[qualifier_value["datavalue"]["value"]["id"]])
                                    else:
                                        print("UNKNOWN BEHAVIOUR FOR QUALIFIER VALUE: " + w_qualifier + "; " + str(qualifier_value["datavalue"]["value"]) + ", getting first element of value dict")
                                        transformed_qualifiers[translated_wikidata_ids[w_qualifier]].append(list(qualifier_value["datavalue"]["value"].values())[0])
                        transformed_data[translated_wikidata_ids[w_property]][-1]["qualifiers"] = transformed_qualifiers

        # TODO:Remove
        #print(json.dumps(transformed_data))
        #f = open("Scholzi.json", "w")
        #f.write(json.dumps(transformed_data) + "\n")
        # TODO:Remove

        return json.dumps(transformed_data)


def collect_twitter_user(t_handle, t_client):
    request_successful = False
    while not request_successful:
        try:
            response = t_client.get_user(username=t_handle,
                                         user_fields=["id", "name", "username", "created_at", "protected", "withheld",
                                                      "location", "url", "description", "verified", "verified_type",
                                                      "profile_image_url", "public_metrics", "pinned_tweet_id"],
                                         user_auth=False)
            if len(response.errors) != 0 and response.errors[0]["title"] == "Not Found Error":
                raise FileNotFoundError("Twitter")
            request_successful = True
        except tweepy.errors.TooManyRequests:
            sleep_interruptible(15 * 60)
        except tweepy.errors.TwitterServerError as t_servErr:
            print("Twitter unavailable: " + str(t_servErr) + ", Waiting...")
            sleep_interruptible(15 * 60)


    # print(response)
    # TODO: Remove
    #print(response)
    #f = open("TwitterScholzi.json", "w")
    #f.writelines("")
    #f = open("TwitterScholzi.json", "a")
    #f.write(json.dumps(response.data.data) + "\n")
    # TODO: Remove

    return json.dumps(response.data.data)


def store_person(wikidata_data, twitter_data):
    collection = db_connection["TwitterWatcher"]["People"]
    doc = None

    wikidata_data = json.loads(wikidata_data)
    twitter_data = json.loads(twitter_data)

    try:
        doc = collection.fetchDocument(wikidata_data["id"])
        print("Person found in database, editing...")
    except pyArangoExceptions.DocumentNotFoundError:
        doc = collection.createDocument()
        print("Creating new Person in database...")

    doc._key = wikidata_data["id"]

    doc["twitter_object"] = twitter_data
    doc["wikidata_object"] = wikidata_data

    doc.save()


def get_id_by_thandle_from_database(t_handle):
    result = db_connection["TwitterWatcher"].AQLQuery(query='FOR person IN People FILTER person.twitter_object.username == "'
                                                            + t_handle + '" RETURN person._key',
                                                      rawResults=True)
    if len(result) == 0:
        return None
    return result[0]


def get_id_by_tid_from_database(t_id):
    result = db_connection["TwitterWatcher"].AQLQuery(query='FOR person IN People FILTER person.twitter_object.id == "'
                                                            + str(t_id) + '" RETURN person._key',
                                                      rawResults=True)
    if len(result) == 0:
        return None
    return result[0]


def get_first_tweet_edge_from_database(t_id):
    found_doc = None
    try:
        found_doc = db_connection["TwitterWatcher"]["Retweets"].fetchDocument(str(t_id) + "_retweeted")
    except pyArangoExceptions.DocumentNotFoundError:
        try:
            found_doc = db_connection["TwitterWatcher"]["QuoteTweets"].fetchDocument(str(t_id) + "_quoted")
        except pyArangoExceptions.DocumentNotFoundError:
            try:
                found_doc = db_connection["TwitterWatcher"]["Replies"].fetchDocument(str(t_id) + "_replied_to")
            except pyArangoExceptions.DocumentNotFoundError:
                found_doc = None

    return found_doc


def create_tweet_document(tweet_json, references=[], sentiment_value=None, avg_botness=None, avg_maliciousness=None, liking_users=None):
    collection = db_connection["TwitterWatcher"]["Tweets"]
    try:
        collection.fetchDocument(tweet_json["id"])
        print("Tweet doc already exists in database, skipping...")
    except pyArangoExceptions.DocumentNotFoundError:
        doc = collection.createDocument()
        doc._key = tweet_json["id"]

        # Retrieve referenced user handles from quotes and replies without querying twitter
        retweeted = ""
        quoted = ""
        replied_to = ""
        simple_mentions = []

        # if tweet_json.get("referenced_tweets") is not None:
        #     if tweet_json["referenced_tweets"][0]["type"] == "retweeted":
        #         retweeted = tweet_json["entities"]["mentions"][0]["username"]
        #     else:
        #         for ref_tweet in tweet_json["referenced_tweets"]:
        #             if ref_tweet["type"] == "quoted":
        #                 if match := re.search("^twitter[.]com/([^/]+)/.*$",
        #                                       tweet_json["entities"]["urls"][-1]["display_url"], re.IGNORECASE):
        #                     quoted = match.group(1)
        #             if ref_tweet["type"] == "replied_to":
        #                 replied_to_id = tweet_json["in_reply_to_user_id"]

        for ref in references:
            if ref[0] == "retweeted":
                retweeted = ref[1]
            elif ref[0] == "quoted":
                quoted = ref[1]
            elif ref[0] == "replied_to":
                replied_to = ref[1]

        if tweet_json.get("entities") is not None and tweet_json["entities"].get("mentions") is not None:
            if retweeted == "":
                for m_index in range(0, len(tweet_json["entities"]["mentions"])):
                    if not (m_index == 0 and replied_to != ""):
                        simple_mentions.append(tweet_json["entities"]["mentions"][m_index]["id"])

        doc["retweeted"] = retweeted
        doc["quoted"] = quoted
        doc["replied_to"] = replied_to
        if len(simple_mentions) != 0:
            doc["mentions"] = simple_mentions

        doc["tweet"] = tweet_json

        if sentiment_value is not None:
            doc["sentiment_value"] = sentiment_value
        if is_not_retweet(tweet_json) and avg_botness is not None:
            doc["avg_response_botness"] = avg_botness
            doc["avg_response_bot_maliciousness"] = avg_maliciousness
        if liking_users is not None:
            doc["liking_users"] = liking_users

        print("Tweet stored")
        doc.save()


def create_tweet_edge(tweet_type, tweet_json, _from_tid, _to_tid, sentiment_value=None, avg_botness=None, avg_maliciousness=None, ref_tweet=None):
    switch_case_print = {"retweeted": "Retweet", "quoted": "Quote tweet", "replied_to": "Reply", "mentioned": "Mention"}
    switch_case = {"retweeted": "Retweets", "quoted": "QuoteTweets", "replied_to": "Replies", "mentioned": "Mentions"}
    collection = db_connection["TwitterWatcher"][switch_case[tweet_type]]


    _from = get_id_by_tid_from_database(_from_tid)
    _to = get_id_by_tid_from_database(_to_tid)
    if _from is not None and _to is not None:
        try: #TODO: Remove this, likely not needed anymore
            if tweet_type != "mentioned":
                collection.fetchDocument(tweet_json["id"] + "_" + tweet_type)
                print(switch_case_print[tweet_type] + " already exists in database, skipping...")
            else:
                collection.fetchDocument(tweet_json["id"] + "_" + tweet_type + "_" + _from + "_" + _to)
                print(switch_case_print[tweet_type] + " already exists in database, skipping...")
        except pyArangoExceptions.DocumentNotFoundError:
            doc = collection.createDocument()

            if tweet_type != "mentioned":
                doc._key = tweet_json["id"] + "_" + tweet_type
            else:  # Mentions need involved people too to have unique ids
                doc._key = tweet_json["id"] + "_" + tweet_type + "_" + _from + "_" + _to

            doc._from = "People/" + _from
            doc._to = "People/" + _to

            doc["tweet_id"] = tweet_json["id"]
            doc["text"] = tweet_json["text"]
            doc["created_at"] = tweet_json["created_at"]
            doc["author_id"] = tweet_json["author_id"]
            doc["edit_history_tweet_ids"] = tweet_json["edit_history_tweet_ids"]
            if tweet_json.get("geo") is not None:
                doc["place_id"] = tweet_json["geo"]["place_id"]

            if ref_tweet is not None:  # If we don't reference any tweet, happens when we mention someone
                doc["referenced_tweet_id"] = ref_tweet["id"]

            if tweet_json.get("entities") is not None:  # Check if there even are hashtags/urls in the text
                if tweet_json["entities"].get("urls") is not None:
                    doc["urls"] = tweet_json["entities"]["urls"]
                if tweet_json["entities"].get("hashtags") is not None:
                    doc["hashtags"] = tweet_json["entities"]["hashtags"]
            if tweet_json.get("withheld") is not None:
                doc["withheld"]["country_codes"] = tweet_json["withheld"]["country_codes"]
                doc["withheld"]["scope"] = tweet_json["withheld"]["scope"]

            doc["public_metrics"] = tweet_json["public_metrics"]
            doc["possibly_sensitive"] = tweet_json["possibly_sensitive"]
            doc["lang"] = tweet_json["lang"]
            #Include tweet app source as well?

            if sentiment_value is not None:
                doc["sentiment_value"] = sentiment_value
            if is_not_retweet(tweet_json) and avg_botness is not None:
                doc["avg_response_botness"] = avg_botness
                doc["avg_response_bot_maliciousness"] = avg_maliciousness

            doc.save()
            print(switch_case_print[tweet_type] + " stored")
    else:
        print("Cant find one of the people for the tweet in database, skipping...")


# TODO: Give info on stored tweets what other kinds it exists in
def store_tweet(t_client, tweet_json, sentiment_value=None, avg_botness=None, avg_maliciousness=None, liking_users=None):
    print("TWEET:" + str(tweet_json))  # TODO: Remove
    references = []
    # Collect Retweet/Quote Tweet/Replies
    if tweet_json.get("referenced_tweets") is not None:
        for ref_tweet in tweet_json["referenced_tweets"]:
            # Look up the id of the replied_to user by its handle
            if ref_tweet["type"] == "retweeted" or ref_tweet["type"] == "quoted":
                request_got_through = False
                while not request_got_through:
                    try:
                        author_lookup_response = t_client.get_tweet(id=ref_tweet["id"], expansions=["author_id"])  #TODO: Remove and just lookup handles here
                        # print(author_lookup_response)
                        refed_user_id = author_lookup_response.includes["users"][0]["id"]
                        references.append((ref_tweet["type"], refed_user_id))
                        request_got_through = True
                    except tweepy.errors.TooManyRequests:
                        sleep_interruptible(15*60)
                    except tweepy.errors.TwitterServerError as servErr:
                        print("Twitter unavailable: " + str(servErr) + ", Waiting...")
                        sleep_interruptible(15 * 60)
            else:
                refed_user_id = tweet_json["in_reply_to_user_id"]
                references.append((ref_tweet["type"], refed_user_id))

            create_tweet_edge(ref_tweet["type"], tweet_json, tweet_json["author_id"], str(refed_user_id), sentiment_value,
                              avg_botness, avg_maliciousness, ref_tweet=ref_tweet)
    # Store actual tweet as non-edge for checking new users later
    create_tweet_document(tweet_json, references, sentiment_value, avg_botness, avg_maliciousness, liking_users)

    # Collect Mentions
    if tweet_json.get("entities") is not None and tweet_json["entities"].get("mentions") is not None:
        for mention in tweet_json["entities"]["mentions"]:
            # If this tweet is not a retweet, retweets don't have text and with that no mentions
            if not is_not_retweet(tweet_json):

                # If this tweet is a reply, all users in the conversation exist as mentions
                # We then need to ignore the first mentioned person since this is the one that was replied to
                is_reply = False
                if tweet_json.get("referenced_tweets") is not None:
                    for reference in tweet_json["referenced_tweets"]:
                        if reference["type"] == "replied_to":
                            is_reply = True # This is (also) a reply tweet
                if is_reply and mention["start"] == 0:
                    continue  # Ignore the first mention

                create_tweet_edge("mentioned", tweet_json, tweet_json["author_id"], mention["id"], sentiment_value,
                                  avg_botness, avg_maliciousness, ref_tweet=None)


def create_like_document(liked_user_id, liking_user_id, tweet_id):
    collection = db_connection["TwitterWatcher"]["Likes"]
    try:
        collection.fetchDocument(tweet_id + "_liked_" + liking_user_id + "_" + liked_user_id)
        print("Like already exists in database, skipping...")
    except pyArangoExceptions.DocumentNotFoundError:
        doc = collection.createDocument()
        doc._key = tweet_id + "_liked_" + liking_user_id + "_" + liked_user_id
        doc._from = "People/" + liking_user_id
        doc._to = "People/" + liked_user_id

        doc["tweet_id"] = tweet_id
        doc["sentiment_value"] = 0.5

        doc.save()


# Store edges for people liking others and dump a list of all collected liking users into corresponding tweet doc
def store_likes(t_id, liking_users_in_db, tweet_id):
    liked_user_id = get_id_by_tid_from_database(t_id)
    if liked_user_id is not None:  # TODO: Delete, should not be necessary since tweet author should always exist in db
        for liking_user_id in liking_users_in_db:
            create_like_document(liked_user_id, liking_user_id, tweet_id)
    else:
        print("Can't store likes for unknown user")


# TODO: Use rest of liking users for bot detection as well?
def collect_liking_users(tweet_id, t_client, page_limit=sys.maxsize):
    global savepoint

    liking_users = []
    liking_users_in_db = []
    all_results_found = False
    if savepoint.like_pagination is not None:
        pagination_start = savepoint.like_pagination[0]
        pagination_token = savepoint.like_pagination[1]
    else:
        pagination_start = 0
        pagination_token = None

    for i in range(pagination_start, page_limit):
        if all_results_found:
            break

        request_got_through = False
        while not request_got_through:
            try:
                t_response = t_client.get_liking_users(tweet_id,
                                                       max_results=100,
                                                       pagination_token=pagination_token)
                time.sleep(.01)
                #print(t_response)
                if t_response.meta.get("next_token") is None:
                    all_results_found = True
                else:
                    pagination_token = t_response.meta["next_token"]

                if t_response.data is not None:  # A Tweet can also have no likes, in this case we skip
                    for user in t_response.data:
                        liking_users.append(user.id)
                        data_id = get_id_by_tid_from_database(user.id)
                        if data_id is not None:
                            liking_users_in_db.append(data_id)

                request_got_through = True
            except tweepy.errors.TooManyRequests:
                sleep_interruptible(15*60)
            except tweepy.errors.TwitterServerError as t_servErr:
                print("Twitter unavailable: " + str(t_servErr) + ", Waiting...")
                sleep_interruptible(15*60)

        if config.stop_collection:
            savepoint.like_pagination = (i, pagination_token)
            break

    return liking_users_in_db, liking_users


def get_tweet_sentiment_value(tweet_json):
    analyzer = SentimentIntensityAnalyzer()
    if is_not_retweet(tweet_json):  # Retweets dont have own text
        tweet_text = tweet_json["text"]
        if tweet_json.get("in_reply_to_user_id") is not None:  # Replies have many mentions at the beginning that are not from the actual text, remove them
            tweet_text = re.sub("(@[^ ]* )+", "", tweet_text)
        tweet_text = re.sub("https://[^ ]+", "", tweet_text)  # Remove links
        if tweet_json["lang"] != "en":  # Translate non-english text
            tweet_text = tweet_text.encode('utf-16','surrogatepass').decode('utf-16')  # Transform to utf-16 to cope with emojis
            api_url = "http://mymemory.translated.net/api/get?q={}&langpair={}|en&de=maximilian.kissgen@rwth-aachen.de".format(tweet_text, tweet_json["lang"])
            headers = {
                'User-Agent': 'TwitterWatcher/0.1 maximilian.kissgen@rwth-aachen.de',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.3',
                'Accept-Encoding': 'none',
                'Accept-Language': 'en-US,en;q=0.8',
                'Connection': 'keep-alive'}

            request_successful = False
            while not request_successful:
                translator_response = requests.get(api_url, headers=headers)
                response_json = json.loads(translator_response.text)
                if translator_response.status_code == 429 or json.loads(translator_response.content)["quotaFinished"]:
                    next_day = False
                    while not next_day:  # Wait until next day TODO: Test this
                        sleep_interruptible(60*60)
                        if datetime.now(timezone.utc).hour == 0:
                            next_day = True
                else:
                    request_successful = True
            tweet_text = response_json["responseData"]["translatedText"]

        sentiment_values = {"compound": 0}
        if tweet_json["lang"] != "en" and response_json["responseStatus"] == "403":
            print("TRANSLATION FAILED. Likely invalid language: " + str(tweet_json["lang"]))
        else:
            print("TRANSLATION:" + tweet_text)
            sentiment_values = analyzer.polarity_scores(tweet_text)

        return sentiment_values["compound"]

    else:
        return 0.75


def get_bot_response(user_list):
    bot_detection_db = db_connection["TwitterWatcher"]["UserBotDetectionValues"]

    avg_botness = 0
    avg_maliciousness = 0
    for user in user_list:
        print("Checking user " + user)
        vals_found_in_db = False
        try:
            doc = bot_detection_db.fetchDocument(user)
            print("Bot values found")
            avg_botness += doc["botness"]
            avg_maliciousness += doc["maliciousness"]
            vals_found_in_db = True
        except pyArangoExceptions.DocumentNotFoundError:
            pass

        request_successful = False
        while not vals_found_in_db and not request_successful:
            try:
                headers = {
                    'User-Agent': 'TwitterWatcher/0.1 maximilian.kissgen@rwth-aachen.de',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.3',
                    'Accept-Encoding': 'none',
                    'Accept-Language': 'en-US,en;q=0.8',
                    'Connection': 'keep-alive'}
                response = requests.get("https://milki-psy.dbis.rwth-aachen.de/bot-detector/api/user-check/" + user
                                        , headers=headers, timeout=8)
                response_json = json.loads(response.text)
                avg_botness += response_json["signals"]["is_bot_probability"]
                avg_maliciousness += response_json["signals"]["intentions_are_bad_probability"]

                doc = bot_detection_db.createDocument()
                print("Saving bot values...")
                doc._key = user
                doc["botness"] = response_json["signals"]["is_bot_probability"]
                doc["maliciousness"] = response_json["signals"]["intentions_are_bad_probability"]
                doc.save()

                request_successful = True
            except ReadTimeout:
                print("Bot detector timeout likely cause of rate limit, waiting")
                #TODO Test this
                sleep_interruptible(15*60)
            except ConnectionError as connection_ex:
                print(connection_ex)
                #TODO Test this
                sleep_interruptible(15*60)
            except JSONDecodeError as json_ex:
                print(json_ex)
                print(response.text)
                #TODO Test this
                sleep_interruptible(15*60)

            if config.stop_collection:
                break

        if config.stop_collection:
            break


    avg_botness = avg_botness/len(user_list) if len(user_list) != 0 else 0
    avg_maliciousness = avg_maliciousness/len(user_list) if len(user_list) != 0 else 0

    return avg_botness, avg_maliciousness


def get_responding_users(tweet_data, t_client):
    request_got_through = False
    while not request_got_through:
        try:
            t_response = t_client.search_all_tweets(query="in_reply_to_tweet_id:" + tweet_data["id"] + " OR "
                                                          + "retweets_of_tweet_id:" + tweet_data["id"] + " OR "
                                                          + "quotes_of_tweet_id:" + tweet_data["id"],
                                                    start_time=datetime.fromisoformat(tweet_data["created_at"].replace("Z", "+00:00")),  # fromisoformat() can not parse all variants of iso 8601 utc time, hence the replacing
                                                    end_time=None,#datetime_lib.datetime(2023, 1, 16),
                                                    expansions=["author_id"],
                                                    max_results=100,
                                                    media_fields=None,
                                                    next_token=None,
                                                    place_fields=None,
                                                    poll_fields=None,
                                                    since_id=None,#tweet_data["id"],
                                                    sort_order=None,
                                                    tweet_fields=None,
                                                    until_id=None,
                                                    user_fields=["username"])
            request_got_through = True
            time.sleep(2)  # Mandatory wait between all full active tweet search requests is > 1 second

        except tweepy.errors.TooManyRequests:
            sleep_interruptible(15*60)
        except tweepy.errors.TwitterServerError as servErr:
            print("Twitter unavailable: " + str(servErr) + ", Waiting...")
            sleep_interruptible(15*60)

    users = []
    if t_response.includes.get("users") is not None:  # Sometimes there may be no responses
        for user in t_response.includes["users"]:
            users.append(user.username)

    return users


def is_not_retweet(tweet_json):
    if tweet_json.get("referenced_tweets") is None or tweet_json["referenced_tweets"][0]["type"] != "retweeted":
        return True
    else:
        return False


# TODO: Abort on config.stop_collection is true, Save (current person to be collected, date, tweets) to know where left off
def collect_tweets_by_query(person, t_query, t_client, start_date, end_date, catch_up=False, catch_up_date=config.start_date):
    global savepoint

    tweets = []
    pagination_token = None
    all_results_found = False
    #start_date = config.start_date
    #if catch_up:
    #    print("Catching up")
    #    start_date = catch_up_date
    #    if start_date < config.start_date:
    #        return
    #elif not catch_up and not
    if savepoint.person is not None and person[2] == savepoint.person[2]:  # Start where we left off
        if savepoint.tweets_left is not None:
            tweets = savepoint.tweets_left
        pagination_token = savepoint.pagination_token
    while not all_results_found:
        request_got_through = False
        while not request_got_through:
            try:
                print("from:" + person[3] + t_query)
                t_response = t_client.search_all_tweets(query="from:" + person[3] + t_query,
                                                        start_time=start_date.astimezone(timezone.utc),
                                                        end_time=end_date.astimezone(timezone.utc),
                                                        max_results=500,  # TODO: Change to 500
                                                        media_fields=None,
                                                        next_token=pagination_token,
                                                        place_fields=None,
                                                        poll_fields=None,
                                                        since_id=None,
                                                        sort_order=None,
                                                        tweet_fields=["id", "created_at", "text", "geo", "in_reply_to_user_id",
                                                                  "referenced_tweets", "author_id", "entities", "withheld",
                                                                  "public_metrics", "lang", "possibly_sensitive", "conversation_id"],
                                                        until_id=None,
                                                        user_fields=None)
                request_got_through = True
                time.sleep(2)  # Mandatory wait between all full active tweet search requests is > 1 second
                if t_response.data is not None:
                    for tweet_object in t_response.data:
                        tweets.append(tweet_object.data)
                    #tweets.extend(t_response.data)
                    print("NEED TO GO FURTHER")
                if t_response.meta.get("next_token") is None:
                    pagination_token = None
                    all_results_found = True
                else:
                    pagination_token = t_response.meta["next_token"]

            except tweepy.errors.TooManyRequests:
                sleep_interruptible(15*60)
            except tweepy.errors.TwitterServerError as t_servErr:
                print("Twitter unavailable: " + str(t_servErr) + ", Waiting...")
                sleep_interruptible(15*60)

        if config.stop_collection:
            if not catch_up:
                savepoint.person = person
                savepoint.tweets_left = tweets
                savepoint.pagination_token = pagination_token
                savepoint.query = t_query
            return

    # print(response)
    # TODO: Remove
    print("COLLECTED TWEETS FOR " + str(person[3]) + ":" + str(tweets))
    #f = open("requestOutputAll.json", "w")
    #f.writelines("")
    #f = open("requestOutputAll.json", "a")
    # TODO: Remove
    for index in range(0, len(tweets)):
        sentiment_value, avg_botness, avg_maliciousness = None, None, None
        try:
            found_doc = db_connection["TwitterWatcher"]["Tweets"].fetchDocument(tweets[index]["id"], rawResults=True)
            print("DOC FOUND:" + str(found_doc))
            if found_doc.get("sentiment_value") is not None:
                sentiment_value = found_doc["sentiment_value"]
            elif config.do_sentiment_analysis:
                print("Getting sentiments")
                sentiment_value = get_tweet_sentiment_value(tweets[index])
            if found_doc.get("avg_response_botness") is not None:
                avg_botness = found_doc["avg_response_botness"]
                avg_maliciousness = found_doc["avg_response_bot_maliciousness"]
            elif is_not_retweet(tweets[index]) and config.do_bot_detection:
                print("Getting bots")
                avg_botness, avg_maliciousness = get_bot_response(get_responding_users(tweets[index], t_client))
        except pyArangoExceptions.DocumentNotFoundError:
            if config.do_sentiment_analysis:
                print("Getting sentiments")
                sentiment_value = get_tweet_sentiment_value(tweets[index])
            if is_not_retweet(tweets[index]) and config.do_bot_detection:
                print("Getting bots")
                avg_botness, avg_maliciousness = get_bot_response(get_responding_users(tweets[index], t_client))

        tweet_liking_users = None
        if is_not_retweet(tweets[index]):  # Retweets can't be liked
            print("Getting likes")
            liking_users_in_db, tweet_liking_users = collect_liking_users(tweets[index]["id"], t_client, page_limit=1)
            store_likes(tweets[index]["author_id"], liking_users_in_db, tweets[index]["id"])

        if config.stop_collection:
            if not catch_up:
                savepoint.person = person
                savepoint.tweets_left = tweets[index:]  #TODO: Test this
                savepoint.pagination_token = pagination_token
                savepoint.query = t_query
            return

        print("Storing Tweet")
        store_tweet(t_client, tweets[index], sentiment_value, avg_botness, avg_maliciousness, tweet_liking_users)


def read_people_from_csv(people_csv):
    df = pd.read_csv(people_csv)

    return df


def check_input(input_people):
    if input_people.shape[0] == 0:
        raise Exception("Got empty People File")
    elif input_people.shape[1] > 3:
        raise Exception("Too many columns in People File")
    elif input_people.shape[1] < 3:
        raise Exception("Too few columns in People File")

    for index, person in input_people.iterrows():
        if person[0] == "" and person[1] == "":
            raise Exception("No wikidata info given for " + str(person))
        if person[2] == "":
            raise Exception("No twitter info given for " + str(person))

        wikidata_format = re.compile("^Q[0-9]+$")
        if not wikidata_format.match(person[1]):
            raise Exception("wikidata id format not matched for " + str(person))
        twitter_format = re.compile("^([A-Z]|[a-z]|[0-9]|_)+$")
        if not twitter_format.match(person[2]):
            raise Exception("twitter handle format not matched for " + str(person))


def reset_index_and_update_savepoint_person():
    global people, savepoint

    people.reset_index(inplace=True) # Reorder indexes

    if savepoint.person is not None: # Assign the new index to the current person
        savepoint_person_old_index = savepoint.person[0]
        savepoint_person_new_index = people.loc[people['index'] == savepoint_person_old_index].index.values.astype(int)[0]
        savepoint.person = (savepoint_person_new_index, savepoint.person[1], savepoint.person[2], savepoint.person[3])

    people.drop(columns=["index"], inplace=True)  # drop the extra index column created by pandas


def collect_people(t_client, people):
    remove_people = []

    for person in people.itertuples(name=None, index=True):
        try:
            # try:
            #     db_connection["TwitterWatcher"]["People"].fetchDocument(person[1])
            # except pyArangoExceptions.DocumentNotFoundError:
            wikidata_data = collect_person_from_wikidata(person[1], person[2])
            twitter_data = collect_twitter_user(person[3], t_client)
            print(person[2], person[3])
            store_person(wikidata_data, twitter_data)
        except FileNotFoundError as person_ex:
            print("Person not found on " + str(person_ex) + "\n" + str(person) +"\nRemoving from list and skipping them...")

            remove_people.append(person[0])

        if config.stop_collection:
            break

    if len(remove_people) != 0:
        people.drop(index=remove_people, inplace=True)
        reset_index_and_update_savepoint_person()

    return people


def find_referencing_tweets(person_t_id):
    return db_connection["TwitterWatcher"].AQLQuery('FOR tweet IN Tweets'
                                                                    + ' FILTER (tweet.retweeted == ' + str(person_t_id)
                                                                    + '    OR tweet.quoted == ' + str(person_t_id)
                                                                    + '    OR tweet.replied_to == ' + str(person_t_id)
                                                                    + '    OR tweet.mentions != null AND POSITION(tweet.mentions, ' + str(person_t_id) + ")"
                                                                    + '    OR tweet.likes != null AND POSITION(tweet.likes, ' + str(person_t_id) + ")"
                                                                    + '    )'
                                                                    + ' RETURN tweet._key'
                                                              , batchSize=100000, rawResults=True)


def catch_up_new_people(new_people):
    """Function to search existing tweets for references of newly added people and to save them as edges"""

    for person in new_people.itertuples(name=None, index=False):
        # Check database for tweets that referenced/mentioned the newly added people
        person_t_id = db_connection["TwitterWatcher"]["People"].fetchDocument(person[1])["twitter_object"]["id"]
        print("Catching up " + str(person[1]))
        ref_tweet_ids = find_referencing_tweets(person_t_id)
        for tweet_id in ref_tweet_ids:
            tweet_doc = db_connection["TwitterWatcher"]["Tweets"].fetchDocument(str(tweet_id))
            print("Tweet: " + str(tweet_doc["tweet"]))
            tweet_json = tweet_doc["tweet"]
            sentiment_value, avg_botness, avg_maliciousness = None, None, None
            if tweet_doc["sentiment_value"] is not None:
                sentiment_value = tweet_doc["sentiment_value"]
            if tweet_doc["avg_response_botness"] is not None:
                avg_botness = tweet_doc["avg_response_botness"]
                avg_maliciousness = tweet_doc["avg_response_bot_maliciousness"]

            if tweet_doc["retweeted"] == person_t_id:
                create_tweet_edge("retweeted", tweet_json, tweet_json["author_id"], person_t_id, sentiment_value,
                                  avg_botness, avg_maliciousness, tweet_json["referenced_tweets"][0]["id"])
            else:
                for ref_tweet in tweet_json["referenced_tweets"]:
                    if ref_tweet["type"] == "quoted":
                        create_tweet_edge("quoted", tweet_json, tweet_json["author_id"], person_t_id, sentiment_value,
                                          avg_botness, avg_maliciousness, ref_tweet["id"])
                    elif ref_tweet["type"] == "replied_to":
                        create_tweet_edge("replied_to", tweet_json, tweet_json["author_id"], person_t_id,
                                          sentiment_value, avg_botness, avg_maliciousness, ref_tweet["id"])
                if tweet_doc["mentions"] is not None and person_t_id in tweet_doc["mentions"]:
                    create_tweet_edge("mentioned", tweet_json, tweet_json["author_id"], person_t_id, sentiment_value,
                                      avg_botness, avg_maliciousness)
                if tweet_doc["liking_users"] is not None and person_t_id in tweet_doc["liking_users"]:
                    create_like_document(tweet_json["author_id"], person_t_id, tweet_id)

            if config.stop_collection:
                return
        if config.stop_collection:
            return


def incr_date_by_timestep(date, time_step_size):
    incr_date = date
    if time_step_size == config.Timesteps.NO_STEPS:
        incr_date = config.end_date
    elif time_step_size == config.Timesteps.MONTHS:
        incr_date += relativedelta(months=+1)
    elif time_step_size == config.Timesteps.WEEKS:
        incr_date += relativedelta(weeks=+1)
    elif time_step_size == config.Timesteps.DAYS:
        incr_date += relativedelta(days=+1)
    else:
        raise Exception("Got no valid timesteps value for date incrementation")

    return incr_date


def load_savepoint():
    global savepoint, queries, people

    with open('savepoint/savepoint.json', 'r', encoding="utf-8") as file:
        savepoint_json = json.loads(file.read())
        savepoint.person = tuple(savepoint_json["savepoint_person"]) if savepoint_json["savepoint_person"] is not None else None
        savepoint.query = savepoint_json["savepoint_query"]
        savepoint.tweets_left = savepoint_json["savepoint_tweets_left"]
        savepoint.pagination_token = savepoint_json["savepoint_pagination_token"]
        savepoint.like_pagination = tuple(savepoint_json["savepoint_like_pagination"]) if savepoint_json["savepoint_like_pagination"] is not None else None
        savepoint.current_start_date = datetime.strptime(savepoint_json["savepoint_current_start_date"], '%Y-%m-%d %H:%M:%S') if savepoint_json["savepoint_current_start_date"] is not None else None

        config.end_date = datetime.strptime(savepoint_json["end_date"], '%Y-%m-%d %H:%M:%S') if savepoint_json["end_date"] is not None else None
        config.time_step_size = config.Timesteps(int(savepoint_json["time_step_size"]))
        config.do_sentiment_analysis = savepoint_json["do_sentiment_analysis"]
        config.do_bot_detection = savepoint_json["do_bot_detection"]
        queries = savepoint_json["queries"]
        if savepoint_json["added_filters"] is not None:
            config.added_filters = dict(savepoint_json["added_filters"])

    config.people = pd.read_csv(filepath_or_buffer='savepoint/people.csv')
    if os.path.isfile("./savepoint/added_people.csv"):
        config.added_people = pd.read_csv(filepath_or_buffer='savepoint/added_people.csv')

    print("Savepoint loaded...")
    #print(savepoint.person, "\n", savepoint.query, "\n", savepoint.tweets_left, "\n", savepoint.pagination_token, "\n", savepoint.like_pagination, "\n", savepoint.start_date)


def store_savepoint():
    global savepoint

    # First save the actual collection savepoint
    with open('savepoint/savepoint.json', 'w', encoding="utf-8") as file:
        file.write("")
    with open('savepoint/savepoint.json', 'a', encoding="utf-8") as file:
        savepoint_str = ("{\n")
        savepoint_str += ('"savepoint_person": '
                          + (json.dumps(list(savepoint.person)) if savepoint.person is not None else "null")
                          + ",\n")
        savepoint_str += ('"savepoint_query": '
                          + ('"' + str(savepoint.query) + '"' if savepoint.query is not None else "null")
                          + ",\n")
        savepoint_str += ('"savepoint_tweets_left": '
                          + (json.dumps(savepoint.tweets_left) if savepoint.tweets_left is not None else "null")
                          + ",\n")
        savepoint_str += ('"savepoint_pagination_token": '
                          + ('"' + str(savepoint.pagination_token) + '"' if savepoint.pagination_token is not None else "null")
                          + ",\n")
        savepoint_str += ('"savepoint_like_pagination": '
                          + (json.dumps(list(savepoint.like_pagination)) if savepoint.like_pagination is not None else "null")
                          + ",\n")
        savepoint_str += ('"savepoint_current_start_date": '
                          + ('"' + str(savepoint.current_start_date) + '"' if savepoint.current_start_date is not None else "null")
                          + ",\n")
        savepoint_str += ('"end_date": '
                          + ('"' + str(config.end_date) + '"' if config.end_date is not None else "null")
                          + ",\n")
        savepoint_str += ('"time_step_size": '
                          + ('"' + str(config.time_step_size.value) + '"' if config.time_step_size is not None else "null")
                          + ",\n")
        # Additionally save the queries we use and whether we do bot detection or sentiment analysis
        savepoint_str += ('"queries": ' + json.dumps(queries) + ",\n") #if queries is not None else "null"
        savepoint_str += ('"do_bot_detection": ' + json.dumps(config.do_bot_detection) + ",\n")
        savepoint_str += ('"do_sentiment_analysis": ' + json.dumps(config.do_sentiment_analysis) + ",\n")
        # Save filter keywords if their adding process was not yet completed
        savepoint_str += '"added_filters": ' + (json.dumps(config.added_filters) if config.added_filters is not None else "null") + "\n"
        savepoint_str += "}"
        #print(json.dumps(ast.literal_eval(savepoint)))
        #savepoint = json.dumps(ast.literal_eval(savepoint))
        #Sometimes tweet texts alternate between ' or " when printed, this is to make it uniform (and also work better with editors like notepad++)
        #savepoint = savepoint.replace("\'", "\"")

        file.write(savepoint_str)

    #with open('savepoint/savepoint.json', 'r', encoding="utf-8") as file:
    #    print("FILE\n" + str(json.loads(file.read())))

    # Then store the people we collect data for
    with open('savepoint/people.csv', 'w', encoding="utf-8") as file:
        people.to_csv(path_or_buf=file, index=False, lineterminator="\n")

    # Store edits for the people object if we did not get done with them
    if config.added_people is not None:
        with open('savepoint/added_people.csv', 'w', encoding="utf-8") as file:
            people.to_csv(path_or_buf=file, index=False, lineterminator="\n")

    print("Savepoint created...")


def delete_savepoint():
    global savepoint

    savepoint.person = None
    savepoint.tweets_left = None
    savepoint.current_start_date = None
    savepoint.query = None
    savepoint.like_pagination = None
    savepoint.pagination_token = None


def stop_collection_process():
    config.stop_collection = True
    exit_sleep.set()


# TODO: Remove spaces for wikidata object keys?
def collection(use_savepoint=False):
    global people, queries, current_person, collecting_people, current_start_date, current_end_date,\
           savepoint

    setup_database()  # TODO: Start this up on website ini, not on collection ini

    t_client = tweepy.Client(bearer_token=config.bearer, return_type=tweepy.Response, wait_on_rate_limit=False)

    config.stop_collection = False
    config.collection_running = True

    people_check_needed = False  # TODO: Set to true
    last_people_check_time = datetime.now(timezone.utc)

    if use_savepoint:
        # TODO: Debug, remove
        if savepoint.person is not None:
            print("SAVEPOINT USED:")
            print(savepoint.person)
            print("QUERY:", savepoint.query)
            print("TWEETS:", savepoint.tweets_left)
            print("PAGINATION:", savepoint.pagination_token)
            print("LIKE_PAGINATION:", savepoint.like_pagination)

        load_savepoint()
        people = config.people
        current_start_date = savepoint.current_start_date
    else:
        delete_savepoint()
        people = config.people
        queries = build_queries(config.tweetEmojis, config.tweetWords, config.tweetHashtags, config.tweetHandles)
        current_start_date = config.start_date
    #check_input(people)

    savepoint.current_start_date = None
    end_date_incr = incr_date_by_timestep(current_start_date, config.time_step_size)
    current_end_date = None
    if (config.end_date is None or config.end_date.date() > datetime.today().date()) \
       and config.time_step_size == config.Timesteps.NO_STEPS:
        raise Exception("No end date given/End date is in future and no increment")
    if config.end_date is None or end_date_incr.date() <= config.end_date.date():
        current_end_date = end_date_incr
    else:
        current_end_date = config.end_date

    end_date_reached = False
    while not end_date_reached:
        if config.added_people is not None:
            collect_people(t_client, config.added_people)

            # Change existing people according to new data and then delete them from added people
            existing_people_indexes = []
            for new_person in config.added_people.itertuples(name=None):
                found_val = people.loc[people['Name'] == new_person[1]]
                if not found_val.empty:
                    people.loc[found_val.index.values.astype(int)[0]] = new_person[1],new_person[2],new_person[3]
                    existing_people_indexes.append(new_person[0])

            added_people_trimmed = pd.DataFrame()  # Extra variable here to not touch added_people for the savepoint
            for index in existing_people_indexes:
                added_people_trimmed = config.added_people.drop(index)


            catch_up_new_people(added_people_trimmed)

            if not config.stop_collection:
                people = pd.concat([people, added_people_trimmed], ignore_index=True) # Add remaining new people
                config.added_people = None

        # Remove People, they will not be considered in the next collection step TODO: Also react to stop_collection here? Would be difficult and removing usually doesnt take ages
        if config.removed_people is not None:
            #print("SHAPE: " + str(people.shape)) #TODO: Remove
            for person_to_remove in config.removed_people.itertuples(name=None, index=True):
                found_val = pd.DataFrame()
                if person_to_remove[2] != "":
                    found_val = people.loc[people['WikidataID'] == person_to_remove[2]]
                if found_val.empty and person_to_remove[2] != "":
                    found_val = people.loc[people['TwitterHandle'] == person_to_remove[3]]
                if found_val.empty and person_to_remove[1] != "":
                    found_val = people.loc[people['Name'] == person_to_remove[1]]

                if found_val.empty:  # Should (usually) not happen
                    print("No valid values given for person to remove, skipping")
                else:
                    print("Removing person:")
                    print(found_val)
                    if savepoint.person is not None:
                        if found_val.iat[0,1] == savepoint.person[2]:  # If we have the current person
                            savepoint.tweets_left = None
                            if not people.iloc[[-1]].iat[0,1] == found_val.iat[0,1]:  # If we don't have the last person
                                next_person = people.iloc[[found_val.index.values.astype(int)[0]+1]]
                                print("Setting next person as savepoint cause current is removed")
                                print(next_person)
                                savepoint.person = (next_person.index.values.astype(int)[0],
                                                  next_person.iat[0,0], next_person.iat[0,1], next_person.iat[0,2])

                            else:
                                print("Removing savepoint cause removed person was last person in line")
                                savepoint.person = None
                            savepoint.pagination_token = None
                            savepoint.query = None
                            savepoint.like_pagination = None

                    people.drop(found_val.index.values.astype(int)[0], inplace=True) # Have to delete them individually else current person might be none

            reset_index_and_update_savepoint_person()

            #print("SHAPE NOW: " + str(people.shape)) #TODO: Remove
            config.removed_people = None


        # Update/create the people documents
        if people_check_needed:
            collecting_people = True
            collect_people(t_client, people)
            collecting_people = False
            people_check_needed = False
            last_people_check_time = datetime.now(timezone.utc)


        # If the collection end date lies in the future (and thus many tweets have not been written yet), wait until another timestep can be made
        if current_end_date.date() >= datetime.now().date():
            time_diff = current_end_date-datetime.now()
            sleep_interruptible(time_diff.seconds + 1000)  # wait some extra seconds to not land in this condition again
            if config.stop_collection:
                savepoint.current_start_date = current_start_date
                store_savepoint()
                config.collection_running = False
                return


        # TODO: Still a bit dumb, refactor so that filters are added to existing queries (or that existing filters must not occur during catchup to not doubly search tweets), maybe also offer possibility of date from when catch up should begin
        # Add new filters
        if config.added_filters is not None:
            print("Adding filters")
            catch_up_queries = build_queries(config.added_filters["emojis"], config.added_filters["keywords"], config.added_filters["hashtags"], config.added_filters["handles"])
            if savepoint.person is not None and current_start_date.date() != config.start_date.date():
                for person in people.itertuples(name=None, index=True):
                    for catchup_query in catch_up_queries:
                        collect_tweets_by_query(person, catchup_query, t_client, catch_up=True, start_date=config.start_date, end_date=current_start_date) #TODO: make catch_up date settable
                        if person[2] == savepoint.person[2] and catchup_query == savepoint.query: # TODO: Check if this can even ever happen.
                            break
                    if person[2] == savepoint.person[2]:
                        break

            if not config.stop_collection:
                config.tweetEmojis.extend(config.added_filters["emojis"])
                config.tweetWords.extend(config.added_filters["keywords"])
                config.tweetHashtags.extend(config.added_filters["hashtags"])
                config.tweetHandles.extend(config.added_filters["handles"])
                queries = build_queries(config.tweetEmojis, config.tweetWords,config.tweetHashtags, config.tweetHandles)#.extend(catch_up_queries)  # Add new queries to existing set

                savepoint.tweets_left = None
                savepoint.pagination_token = None
                savepoint.query = None
                savepoint.like_pagination = None

                config.added_filters = None

        # Remove filters, they will not be considered in the next collection step
        if config.removed_filters is not None:
            print("Removing filters")
            for remove_emoji in config.removed_filters["emojis"]:
                if remove_emoji in config.tweetEmojis:
                    config.tweetEmojis.remove(remove_emoji)
            for remove_word in config.removed_filters["keywords"]:
                if remove_word in config.tweetWords:
                    config.tweetWords.remove(remove_word)
            for remove_handle in config.removed_filters["handles"]:
                if remove_handle in config.tweetHandles:
                    config.tweetHandles.remove(remove_handle)
            for remove_tag in config.removed_filters["hashtags"]:
                if remove_tag in config.tweetHashtags:
                    config.tweetHashtags.remove(remove_tag)

            queries = build_queries(config.tweetEmojis, config.tweetWords, config.tweetHashtags,
                                    config.tweetHandles)

            savepoint.tweets_left = None
            savepoint.pagination_token = None
            savepoint.query = None
            savepoint.like_pagination = None

            config.removed_filters = None

        # Collect the tweets
        for person in people.itertuples(name=None, index=True):
            if savepoint.person is None or person[2] == savepoint.person[2]:  # Skip people we already collected if we have a savepoint
                current_person = person
                for query in queries:
                    if savepoint.query is None or query == savepoint.query:  # Skip queries we already collected if we have a savepoint
                        if savepoint.person is not None and person[2] == savepoint.person[2] \
                                and (savepoint.query is None or query == savepoint.query):  # We caught up now so we dont need to start off from some savepoint

                            savepoint.tweets_left = None
                            savepoint.person = None
                            savepoint.pagination_token = None
                            savepoint.query = None
                            savepoint.like_pagination = None

                        if config.stop_collection:
                            savepoint.person = person
                            savepoint.query = query
                            break

                        collect_tweets_by_query(person, query, t_client, start_date=current_start_date, end_date=current_end_date)

            if config.stop_collection:
                break

        # A new people check is needed after 24 hours passed
        if not people_check_needed and datetime.now(timezone.utc) - last_people_check_time > timedelta(hours=24):
            people_check_needed = True

        if config.stop_collection:
            savepoint.current_start_date = current_start_date
            store_savepoint()
            config.collection_running = False
            return

        # Increment time frame by one step
        if config.time_step_size != config.Timesteps.NO_STEPS:
            current_start_date = current_end_date
            current_end_date = incr_date_by_timestep(current_end_date, config.time_step_size)
            if current_end_date.date() > datetime.today().date():
                current_end_date = datetime.today()

        # Stop collection when we reach the end date, if it exists
        if config.end_date is not None and current_start_date.date() >= current_end_date.date():
            print("Reached end date, stopping collection...")
            end_date_reached = True

    stop_database() #TODO: Do this on the website not here