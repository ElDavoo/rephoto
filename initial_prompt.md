# Script to create

## Context

I upload my photos and video to Google Photos.  
Normally these media would count towards my google account storage space, but when I upload them through my phone (automatically with google photos backup feature) they do not count towards the used storage.  
So now I have a lot of media in Google Photos that take up storage space, and lot of media that do not.  

## What I want

What I want to have i something (can be a Python script or anything you like) that: 
1. Downloads media from google photos that take up space (while preserving all metadata like location, date, etc).
2. Deletes the remote copy from google photos, so storage space is freed.
3. Copies the media onto my phone (which is connected with ADB or MTP, you can decide which protocol you like more).

Hopefully, the phone will re-upload the media to google photos, but this time they will not take up storage space.  