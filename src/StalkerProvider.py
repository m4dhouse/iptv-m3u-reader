from . import _

from enigma import eDVBDB, eServiceReference
from ServiceReference import ServiceReference
from Components.config import config
import requests
import time, re
from .IPTVProcessor import IPTVProcessor
from .Variables import USER_IPTV_VOD_MOVIES_FILE, USER_AGENT, CATCHUP_STALKER, CATCHUP_STALKER_TEXT

db = eDVBDB.getInstance()


class Channel():
	def __init__(self, id, number, name, cmd, catchup_days, picon):
		self.id = id
		self.number = number
		self.name = name.replace(":", "|").replace("  ", " ").strip()
		self.cmd = cmd
		self.catchup_days = catchup_days
		self.picon = picon


class StalkerProvider(IPTVProcessor):
	def __init__(self):
		IPTVProcessor.__init__(self)
		self.type = "Stalker"
		self.refresh_interval = -1
		self.vod_movies = []
		self.progress_percentage = -1
		self.create_epg = False
		self.catchup_type = CATCHUP_STALKER
		self.play_system_vod = "4097"
		self.play_system_catchup = "4097"
		self.session = requests.Session()
		self.token = None

	def storePlaylistAndGenBouquet(self):
		self.checkForNetwrok()
		session = requests.Session()
		token = self.get_token(session)
		if token:
			genres = self.get_genres(session, token)
			# print("GETTING CHANNELS FOR GENRE %s/%s" % (genres[0]["genre_id"], genres[0]["name"]))
			groups = self.get_all_channels(session, token, genres)
			self.channels_callback(groups)
			self.piconsDownload()
			#threads.deferToThread(self.get_channels, session, token, genres).addCallback(self.channels_callback)

	def channels_callback(self, groups):
		tsid = 1000
		blacklist = self.readBlacklist()
		for group in groups.values():
			services = []
			service_list = group[1]
			if self.ch_order_strategy > 0:
				if self.ch_order_strategy == 1:
					service_list.sort(key=lambda x: int(x.number or "0"))
				else:
					service_list.sort(key=lambda x: x.name)
			for service in service_list:
				surl = service.cmd
				if self.use_provider_tsid:
					tsid = int(service.id)
				catchup_days = service.catchup_days
				if catchup_days:
					surl = self.constructCatchupSuffix(str(catchup_days), surl, CATCHUP_STALKER_TEXT)
				ch_name = service.name
				stype = "1"
				if ("UHD" in ch_name or "4K" in ch_name) and " HD" not in ch_name:
					stype = "1F"
				elif "HD" in ch_name:
					stype = "19"
				sref = self.generateChannelReference(stype, tsid, surl.replace(":", "%3a"), ch_name)
				if not self.use_provider_tsid:
					tsid += 1
				if stream_icon := service.picon:
					if self.picon_gen_strategy == 0:
						self.piconsAdd(stream_icon, ch_name)
					else:
						self.piconsSrefAdd(stream_icon, sref)
				services.append(sref)

			if len(services) > 0:
				bfilename = self.cleanFilename(f"userbouquet.m3uiptv.{self.scheme}.{group[0]}.tv")
				if group[0] in blacklist:
					self.removeBouquet(bfilename)  # remove blacklisted bouquet if already exists
					continue
				provider_name_for_titles = self.iptv_service_provider
				name_case_config = config.plugins.m3uiptv.bouquet_names_case.value
				if name_case_config == 1:
					provider_name_for_titles = provider_name_for_titles.lower()
				elif name_case_config == 2:
					provider_name_for_titles = provider_name_for_titles.upper()
				db.addOrUpdateBouquet(provider_name_for_titles + " - " + group[0], bfilename, services, False)

		if not self.ignore_vod:
			self.getVoDMovies()

		self.bouquetCreated(None)

	def get_token(self, session):
		try:
			url = f"{self.url}/portal.php?type=stb&action=handshake&JsHttpRequest=1-xml"
			cookies = {"mac": self.mac, "stb_lang": "en", "timezone": "Europe/London"}
			headers = {"User-Agent": USER_AGENT}
			response = session.get(url, cookies=cookies, headers=headers)
			token = response.json()["js"]["token"]
			if token:
				return token
		except Exception as ex:
			print("[M3UIPTV] [Stalker] Error getting token: " + str(ex))
			pass

	def get_genres(self, session, token):
		try:
			url = f"{self.url}/portal.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
			cookies = {"mac": self.mac, "stb_lang": "en", "timezone": "Europe/London"}
			headers = {"User-Agent": USER_AGENT, "Authorization": "Bearer " + token}
			response = session.get(url, cookies=cookies, headers=headers)
			genre_data = response.json()["js"]
			if genre_data:
				genres = []
				examples = []
				genres.append({'name': _("All channels"), 'category_type': 'IPTV', 'genre_id': "ALL_CHANNELS"})
				genres.append({'name': _("UNCATEGORIZED"), 'category_type': 'IPTV', 'genre_id': "EMPTY"})
				examples.append(_("UNCATEGORIZED"))
				for i in genre_data:
					gid = i["id"]
					name = i["title"]
					genres.append({'name': name, 'category_type': 'IPTV', 'genre_id': gid})
					if gid != "*":
						examples.append(name)
				self.writeExampleBlacklist(examples)
				return genres
		except Exception as ex:
			print("[M3UIPTV] [Stalker] Error getting genres: " + str(ex))
			pass

	def get_channels_for_group(self, groups, services, session, cookies, headers, genre_id):
		page_number = 1
		blacklist = self.readBlacklist()
		total_services_count = 0
		while True:
			time.sleep(0.05)
			url = f"{self.url}/portal.php?type=itv&action=get_ordered_list&genre={genre_id}&fav=0&p={page_number}&JsHttpRequest=1-xml&from_ch_id=0"
			try:
				response = session.get(url, cookies=cookies, headers=headers)
			except:
				time.sleep(3)
				response = session.get(url, cookies=cookies, headers=headers)
			if response.status_code != 200:
				time.sleep(3)
				response = session.get(url, cookies=cookies, headers=headers)

			if response.status_code == 200:
				# print("[M3UIPTV] GETTING CHANNELS FOR PAGE %d" % page_number)
				try:
					response_json = response.json()
					channels_data = response_json["js"]["data"]
					for channel in channels_data:
						surl = f"{self.scheme}%3a//{channel['id']}?cmd={channel['cmd'].replace('ffmpeg ', '').replace('&','|amp|').replace(':', '%3a')}"
						if self.create_bouquets_strategy > 0:  # config option here: for user-optional, all-channels bouquet
							if genre_id not in groups or groups[genre_id][0] not in blacklist:
								groups["ALL_CHANNELS"][1].append(Channel(channel["id"], channel["number"], channel["name"], surl, channel["tv_archive_duration"], channel["logo"]))
						if self.create_bouquets_strategy != 1:  # config option here: for sections bouquets
							services.append(Channel(channel["id"], channel["number"], channel["name"], surl, channel["tv_archive_duration"], channel["logo"]))
						total_services_count += 1

					total_items = response_json["js"]["total_items"]
					if total_services_count >= total_items:
						break
					page_number += 1
				except ValueError:
					print("[M3UIPTV] [Stalker] Invalid JSON format in response")
			else:
				print(f"[M3UIPTV] [Stalker] IPTV Request failed for page {page_number}")
	
	def get_all_channels(self, session, token, genres):
		groups = {} 
		censored_groups = []
		blacklist = self.readBlacklist()
		for group in genres:
			groups[group["genre_id"]] = (group["name"], [])
			censored = False
			try:
				censored = group["censored"] == "1"
			except:
				pass
			if "adult" in group["name"].lower() or "sex" in group["name"].lower() or "xxx" in group["name"].lower() or censored:
				censored_groups.append(group["genre_id"])

		cookies = {"mac": self.mac, "stb_lang": "en", "timezone": "Europe/London"}
		headers = {"User-Agent": USER_AGENT, "Authorization": "Bearer " + token}
		url = f"{self.url}/portal.php?type=itv&action=get_all_channels&JsHttpRequest=1-xml"
		response = session.get(url, cookies=cookies, headers=headers)
		channel_data = response.json()["js"]['data']
		for channel in channel_data:
			surl = f"{self.scheme}%3a//{channel['id']}?cmd={channel['cmd'].replace('ffmpeg ', '').replace('&','|amp|').replace(':', '%3a')}"
			if self.output_format == "ts":
				surl = surl.replace("extension=m3u8", "extension=ts")
			elif self.output_format == "m3u8":
				surl = surl.replace("extension=ts", "extension=m3u8")
			if genre_id := channel["tv_genre_id"]:
				category_id = genre_id
				if self.create_bouquets_strategy > 0:  # config option here: for user-optional, all-channels bouquet
					if category_id not in groups or groups[category_id][0] not in blacklist:
						groups["ALL_CHANNELS"][1].append(Channel(channel["id"], channel["number"], channel["name"], surl, channel["tv_archive_duration"], channel["logo"]))

				if self.create_bouquets_strategy != 1:  # config option here: for sections bouquets
					groups[category_id if category_id and category_id in groups else "EMPTY"][1].append(Channel(channel["id"], channel["number"], channel["name"], surl, channel["tv_archive_duration"], channel["logo"]))
		
		for censored_group in censored_groups:
			self.get_channels_for_group(groups, groups[censored_group][1], session, cookies, headers, censored_group)

		return groups
	
	def get_stream_play_url(self, cmd, session, token):
		cookies = {"mac": self.mac, "stb_lang": "en", "timezone": "Europe/London"}
		headers = {"User-Agent": USER_AGENT, "Authorization": "Bearer " + token}
		url = f"{self.url}/portal.php?type=itv&action=create_link&cmd={cmd}&series=&forced_storage=undefined&disable_ad=0&download=0&JsHttpRequest=1-xml"
		response = session.get(url, cookies=cookies, headers=headers)
		stream_data = response.json()["js"]
		return stream_data["cmd"]
	
	def processService(self, nref, iptvinfodata, callback=None, event=None):
		cmd = ""
		splittedRef = nref.toString().split(":")
		sRef = nref and ServiceReference(nref.toString())
		orig_name = sRef and sRef.getServiceName()
		origRef = ":".join(splittedRef[:10])
		nnref = nref
		match = re.search(r"(?:cmd=)([^&]+)", iptvinfodata)
		if match:
			cmd = match.group(1)

		match = re.search(r"catchupdays=(\d+)", iptvinfodata)
		catchup_days = ""
		if match:
			catchup_days = match.group(1)
	
		if "localhost/ch" not in cmd:
			surl = cmd.replace(":", "%3a").replace("|amp|", "&")
			surl = self.constructCatchupSuffix(catchup_days, surl, CATCHUP_STALKER_TEXT)
			nref_new = origRef + ":" + surl + ":" + orig_name + "•" + self.iptv_service_provider
			nnref = eServiceReference(nref_new)
			self.isPlayBackup = False
			if callback:
				callback(nnref)
			return nnref, nref, False
		self.checkForNetwrok()
		if not self.token:
			self.token = self.get_token(self.session)
		if self.token:
			iptv_url = self.get_stream_play_url(cmd.replace("%3a", ":").replace("|amp|", "&"), self.session, self.token)
			if catchup_days:
				iptv_url = self.constructCatchupSuffix(catchup_days, iptv_url, CATCHUP_STALKER_TEXT)

			if self.output_format == "ts":
				iptv_url = iptv_url.replace("extension=m3u8", "extension=ts")
			elif self.output_format == "m3u8":
				iptv_url = iptv_url.replace("extension=ts", "extension=m3u8")
			nref_new = origRef + ":" + iptv_url.replace(":", "%3a").replace("ffmpeg ", "") + ":" + orig_name + "•" + self.iptv_service_provider
			nnref = eServiceReference(nref_new)
			self.isPlayBackup = False
		if callback:
			callback(nnref)
		return nnref, nref, False

# 	def getVoDMovies(self):
# 		self.vod_movies = []
# 		url = "%s/player_api.php?username=%s&password=%s&action=get_vod_streams" % (self.url, self.username, self.password)
# 		dest_file = USER_IPTV_VOD_MOVIES_FILE % self.scheme
# 		json_string = self.getUrlToFile(url, dest_file)
# 		self.makeVodListFromJson(json_string)

	def loadVoDMoviesFromFile(self):
		self.vod_movies = []
		vodFile = USER_IPTV_VOD_MOVIES_FILE % self.scheme
		json_string = self.loadFromFile(vodFile)
		self.makeVodListFromJson(json_string)
