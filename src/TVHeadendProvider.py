from enigma import eDVBDB
from Components.config import config
import urllib, re, base64
from .IPTVProcessor import IPTVProcessor
from .Variables import CATCHUP_TYPES, USER_AGENT, CATCHUP_DEFAULT

db = eDVBDB.getInstance()


class TVHeadendProvider(IPTVProcessor):
	def __init__(self):
		IPTVProcessor.__init__(self)
		self.type = "TVH"
		self.refresh_interval = -1
		self.vod_movies = []
		self.progress_percentage = -1
		self.create_epg = True
		self.catchup_type = CATCHUP_DEFAULT
		self.play_system_vod = "4097"
		self.play_system_catchup = "4097"

	def constructRequest(self, url):
		headers = {'User-Agent': USER_AGENT}
		headers["Authorization"] = "Basic %s" % base64.b64encode(bytes("%s:%s" % (self.username, self.password), "ascii")).decode("utf-8")
		if "http://" not in url and "https://" not in url:
			url = "http://" + url
		req = urllib.request.Request(url, headers=headers)
		return req

	def getEpgUrl(self):
		url = self.url
		if "http://" not in url and "https://" not in url:
			url = "http://" + url
		return self.custom_xmltv_url if self.is_custom_xmltv and self.custom_xmltv_url else "%s/xmltv/channels/epg.xml" % (url.replace("://", "://%s:%s@" % (self.username, self.password)))

	def storePlaylistAndGenBouquet(self):
		playlist = None
		self.checkForNetwrok()
		url = "%s/playlist/auth/channels.m3u" % self.url
		req = self.constructRequest(url)
		req_timeout_val = config.plugins.m3uiptv.req_timeout.value
		if req_timeout_val != "off":
			response = urllib.request.urlopen(req, timeout=int(req_timeout_val))
		else:
			response = urllib.request.urlopen(req, timeout=10)  # set a timeout to prevent blocking
		playlist = response.read().decode('utf-8')

		self.playlist = playlist
		playlist_splitted = playlist.splitlines()
		tsid = 1000
		services = []
		groups = {}
		line_nr = 0
		captchup_days = ""
		curr_group = None
		for line in playlist_splitted:
			if line.startswith("#EXTINF:"):
				gr_match = re.search(r"group-title=\"(.*?)\"", line)
				if gr_match:
					curr_group = gr_match.group(1)
					if curr_group not in groups:
						groups[curr_group] = []
				else:
					curr_group = None
				epg_id = "None"
				epg_id_match = re.search(r"tvg-id=\"(.*?)\"", line)
				if epg_id_match:
					epg_id = epg_id_match.group(1)
				condition = re.escape(self.search_criteria).replace("\\{SID\\}", "(.*?)") + r".*,(.*)"
				match = re.search(condition, line)
				isFallbackMatch = False
				if not match:
					# Probably the format of the playlist is not m3u+ or for some reason it doesnt contain
					# tvg-id, tvg-name and other similar tags. In this case try matching by the name of service
					condition = r".*,(.*)"
					match = re.search(condition, line)
					isFallbackMatch = True
				if match:
					sid = match.group(1).replace(":", "%3a")
					ch_name = match.group(2) if not isFallbackMatch else sid
					if not sid:
						sid = ch_name.replace(":", "%3a")
					url = ""
					match = re.search(r"tvg-rec=\"(\d.*?)\"", line)
					if not match:
						match = re.search(r"catchup-days=\"(\d.*?)\"", line)
					if not match:
						match = re.search(r"timeshift=\"(\d.*?)\"", line)
					if match:
						captchup_days = match.group(1)
					if self.static_urls or self.isLocalPlaylist():
						found_url = False
						next_line_nr = line_nr + 1
						while not found_url:
							if len(playlist_splitted) > next_line_nr:
								next_line = playlist_splitted[next_line_nr].strip()
								if next_line.startswith("#EXTGRP:") and curr_group is None:  # only if no group was found in #EXTINF: group-title
									curr_group = next_line[8:].strip()
									if curr_group not in groups:
										groups[curr_group] = []
								if next_line.startswith(("http://", "https://")):
									url = next_line.replace(":", "%3a")
									url = self.constructCatchupSufix(captchup_days, url, CATCHUP_TYPES[self.catchup_type])
									captchup_days = ""
									found_url = True
								else:
									next_line_nr += 1
							else:
								break
					else:
						url = self.scheme + "%3a//" + sid
						url = self.constructCatchupSufix(captchup_days, url, CATCHUP_TYPES[self.catchup_type])
						captchup_days = ""
					stype = "1"
					if "UHD" in ch_name or "4K" in ch_name:
						stype = "1F"
					elif "HD" in ch_name:
						stype = "19"
					sref = self.generateChannelReference(stype, tsid, url.replace(":", "%3a"), ch_name)
					tsid += 1
					if curr_group:
						groups[curr_group].append((sref, epg_id, ch_name))
					else:
						services.append((sref, epg_id, ch_name))
					if "tvg-logo" in line and (stream_icon_match := re.search(r"tvg-logo=\"(.+?)\"", line)):
						self.piconsAdd(stream_icon_match.group(1), ch_name)
			line_nr += 1

		examples = []
		blacklist = self.readBlacklist()

		groups_for_epg = {}  # mimic format used in XtreemProvider.py
		for groupName, srefs in groups.items():
			examples.append(groupName)
			if len(srefs) > 0:
				bfilename = self.cleanFilename(f"userbouquet.m3uiptv.{self.scheme}.{groupName}.tv")
				if groupName in blacklist:
					self.removeBouquet(bfilename)  # remove blacklisted bouquet if already exists
					continue
				db.addOrUpdateBouquet(self.iptv_service_provider.upper() + " - " + groupName, bfilename, [sref[0] for sref in srefs], False)
				groups_for_epg[groupName] = (groupName, srefs)

		if len(services) > 0:
			if len(groups) > 0:
				examples.append("UNCATEGORIZED")
				bfilename = self.cleanFilename(f"userbouquet.m3uiptv.{self.scheme}.UNCATEGORIZED.tv")
				if "UNCATEGORIZED" in blacklist:
					self.removeBouquet(bfilename)  # remove blacklisted bouquet if already exists
				else:
					db.addOrUpdateBouquet(self.iptv_service_provider.upper() + " - UNCATEGORIZED", bfilename, [sref[0] for sref in services], False)
			else:
				bfilename = self.cleanFilename(f"userbouquet.m3uiptv.{self.scheme}.tv")
				db.addOrUpdateBouquet(self.iptv_service_provider.upper(), bfilename, [sref[0] for sref in services], False)
			groups_for_epg["EMPTY"] = ("UNCATEGORIZED", services)
		self.writeExampleBlacklist(examples)
		self.piconsDownload()
		self.generateEPGImportFiles(groups_for_epg)
		self.bouquetCreated(None)