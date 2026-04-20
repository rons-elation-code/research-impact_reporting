---
id: 36410
slug: "how-nonprofits-can-create-track-conversions-in-google-analytics-4"
title: "How Nonprofits Can Create & Track Conversions in Google Analytics 4"
url: "https://www.nptechforgood.com/2023/07/09/how-nonprofits-can-create-track-conversions-in-google-analytics-4/"
date: "2023-07-09T20:54:00"
modified: "2023-09-02T22:05:57"
categories: [18, 101, 86]
category_slugs: ["fundraising", "google", "guest-post"]
tags: []
author: 1
primary_type: "guest"
types: ["guest"]
is_marketing: false
classification_signals: ["category:guest-post"]
sponsor_paragraphs_removed: 0
image_count: 26
---
# How Nonprofits Can Create & Track Conversions in Google Analytics 4

> Accurately measuring the performance of marketing efforts is crucial for nonprofits to evaluate and optimize their strategies to make the most of precious resources. Learning how to create and track Conversions in Google Analytics 4 is a must for today’s digital marketers.

**By [Jo Boyle](https://www.linkedin.com/in/joboyledigital/) – a digital marketing expert with a wealth of experience in nonprofit fundraising and marketing. She shares fundraising resources and guides on [Karma Campaigns](https://karmacampaigns.com/) and offers [Google Analytics and Google Ads](https://karmacampaigns.com/google-ad-grant-management/), [Facebook Ads](https://karmacampaigns.com/facebook-ads-agency/), and [lead generation](https://karmacampaigns.com/lead-generation-for-charites/) services.**

---

It’s official. Universal Analytics (UA) has stopped processing data. Google Analytics 4 (GA4) presents some exciting new opportunities. To make the most of new features, nonprofits must know how to create and track conversions in GA4.

Accurately measuring the performance of marketing efforts is crucial for nonprofits to evaluate and optimise their strategies to make the most of precious resources.

Setting up conversion tracking is also essential for nonprofits to maximise their [Google Ad Grant](https://karmacampaigns.com/blog/google-ad-grants-guide/).

There are a couple of key pieces of information nonprofits need to evaluate their online marketing efforts:

1. How people arrive at their website.
2. How people are using their website.

This can be achieved by tracking the source of sessions and users with UTMs & setting up Conversions for important events in GA4.

---

## Using UTMs to track how users arrive at a website

#### What is a UTM?

Out of the box, GA4 collects some information about the source of the traffic to a website. In a big change from Universal Analytics, [GA4 reports on traffic and user acquisition.](https://www.analyticsmania.com/post/user-acquisition-vs-traffic-acquisition-in-ga4/)

You can find these reports under Reports > Acquisition:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/01-Acquistions-reports-e1688676078206.png?resize=800%2C410&ssl=1)](https://karmacampaigns.com/)

But unfortunately, Google can’t always tell where traffic has come from and needs us to add extra information to our links to tell it; this extra information is called a UTM. They look like this:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/02-UTM-example-2.png?resize=800%2C138&ssl=1)](https://karmacampaigns.com/)

You can also use UTMs to gather information about the campaign and content.

#### Creating and using UTMs

The first place to start is to ensure your email blasts include UTMs on any links to your website. Most email marketing platforms will have a setting to automatically add UTMs to links; search your platform documentation for “UTMs”.

You can also build UTMs manually, [Google has a tool to make it easy.](https://ga-dev-tools.google/campaign-url-builder/)

You must include the following information:

**Source**: this should be the place the traffic will come from, e.g. facebook, www.example.com, nonprofit\_tech\_mailing\_list  
**Medium**: the marketing medium used, such as email, social, cpc, display, or referral  
**Campaign**: the campaign name, such as christmas\_appeal

You can also include content to differentiate different ads:

**Content**: the ad content, e.g. dog\_picture

Using [Google’s Campaign URL Builder](https://ga-dev-tools.google/campaign-url-builder/), fill in the fields and a link is generated for you to copy and use in your campaigns:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/03-UTM-builder-e1688676785756.png?resize=500%2C394&ssl=1)](https://karmacampaigns.com/)

You can use them in social posts, emails, and on websites, really anywhere that you would like to be able to track and attribute website traffic from.

But, a UTM should only be used externally to your website. Never add a UTM to an internal link on a website.

#### Using UTMs in Meta Ads

If you use UTM in Meta ads, there is a handy tool under the URL option to build a UTM, labelled ‘Build a URL parameter’.

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/04-Build-a-UTM-paramater-Meta-Ads-e1688676811418.png?resize=500%2C376&ssl=1)](https://karmacampaigns.com/)

This gives the option to dynamically populate the site source, which is useful if you are running a campaign across multiple platforms.[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/05-meta_dynamic_paramater-e1688676864960.png?resize=500%2C548&ssl=1)](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/05-meta_dynamic_paramater-e1688676864960.png?ssl=1)

#### Finding UTM data in reports

UTM data will appear in the acquisition reports. By default, it will be sorted by the [Default channel grouping](https://support.google.com/analytics/answer/9756891?hl=en). However, you can sort these reports by source, medium, campaign or content using the dropdown:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/06-Acquisition-report-with-UTMs-copy.jpg?resize=700%2C494&ssl=1)](https://karmacampaigns.com/)

---

## Setting up conversions in Google Analytics 4

Once users arrive on your nonprofit’s website, the next step is to track how they interact with the site.

#### What is a GA4 event?

GA4 tracks user interactions as Events. Every interaction, including page views, are Events. Events have a set of parameters attached to them that provides more detail about the event.

For example, a page view event has parameters that give us the name of the event (page\_view), detail on the URL (page\_location) and page title (page\_title), amongst other things.

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/07-events-and-paramaters-e1688678674464.png?resize=500%2C159&ssl=1)](https://karmacampaigns.com/)

#### What is a GA4 Conversion?

Conversions are Events that are marked in GA4 as a Conversion. That is, we as users tell Google Analytics that certain Events are important to us, and Google then puts those Conversions into specific reports.

So, to create Conversions in GA4, we must set up Events and then mark them as Conversions.

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/08-Mark-event-as-conversion-1-e1688678691389.png?resize=500%2C142&ssl=1)](https://karmacampaigns.com/)

---

## Step-by-step setup of a G4 Conversion

#### 1) Enabling enhanced measurement

The first step is to check what events are being tracked in Enhanced measurement.

Navigate to Admin > Data Streams:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/09-Enhanced-measurement-01.png?resize=800%2C540&ssl=1)](https://karmacampaigns.com/)

Select your website’s data stream:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/10-Select-Data-stream-1.png?resize=800%2C182&ssl=1)](https://karmacampaigns.com/)

Find Events > Enhanced measurement > Select the cog:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/11-Enhanced-measuement-02.png?resize=800%2C205&ssl=1)](https://karmacampaigns.com/)

You will then see a list of Events that GA4 can track with explanations of each Event. You can enable Events with the toggle on the right. Blue indicates on.

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/12-Enhanced-measurement-03-e1688852766187.png?resize=500%2C532&ssl=1)](https://karmacampaigns.com/)

GA4 will begin tracking any Events that display a blue tick. Depending on how much traffic your website gets, it may take a couple of days for Events to be recorded. They will then begin to appear in reports and under the Events tab.

#### 2) Marking Events as Conversions

If you want to track every Event of a certain type as a Conversion, such as every file download, you can mark the Event as a Conversion once as soon as it shows up in the Events tab.

From the Admin menu, navigate to Events and use the toggles on the right to mark Events as Conversions:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/13-Mark-events-as-conversions-1.jpeg?resize=800%2C338&ssl=1)](https://karmacampaigns.com/)

#### 3) Create custom Conversions

You may want to designate just some Events of a certain type as conversions. For example, views of certain pages, particular videos, or downloads of an important file. For this, you need to create Custom events.

Under Admin > Events > select Create event > Nominate the event name and specific parameters that you want to trigger your custom event. For example, if you want to create an event that is triggered by visits to a particular page, such as a thank you page, you would enter the following:

1. Your Custom event name
2. event\_name equals page\_view

Page\_location contains (ignore case) the URL or a unique part of the URL you would like to track.

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/14-thank_you_page_view.png?resize=800%2C528&ssl=1)](https://karmacampaigns.com/)

It’s usually best to use contains (ignore case) rather than equals, as this will still be triggered if there are variations in the capitalisation of your URL, multiple versions of your website or parameters appended. There are more custom event examples at the end of this article.

Once you have created a Custom event, you must mark it as a conversion. You can do this straight away by manually creating a new conversion event:

1. 1. 1. Navigate to Admin > Conversions > New conversion event
      2. Enter the name of the custom event you created (make sure New event name is exactly the same as Custom event name)

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/15-New_conversions.png?resize=800%2C320&ssl=1)](https://karmacampaigns.com/)

---

## Viewing conversion reports

Once you have conversions set up, they will begin to appear in your reports. Unfortunately, you can’t capture any retrospective conversions, so if you have a low number of conversions, you may have to wait a while.

You can find the conversions report under Reports > Engagement > Conversions. This report will give you an overview of conversions.

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/16-Conversions-Tab-3.jpg?resize=880%2C520&ssl=1)](https://karmacampaigns.com/)

If you click on the name of a conversion in this report, you can view details about the source of the session or user. Again, you can sort your data by source, medium, or campaign via the drop-down menu.

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/17-Conversions-by-channel-copy.jpg?resize=611%2C434&ssl=1)](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/17-Conversions-by-channel-copy.jpg?ssl=1)

Conversions will also appear in the Traffic and User acquisition reports (which we looked at earlier with UTMs).

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/18-Traffic-aquisition-report.png?resize=800%2C305&ssl=1)](https://karmacampaigns.com/)

---

[Karma Campaigns](https://karmacampaigns.com/) provides digital marketing services for not-for-profits, social enterprises and socially conscious businesses. Founded by [Jo Boyle](https://www.linkedin.com/in/joboyledigital/), Karma campaigns specializes in [Google Analytics and Google Ads](https://karmacampaigns.com/google-ad-grant-management/), [Facebook Ads](https://karmacampaigns.com/facebook-ads-agency/), and [lead generation](https://karmacampaigns.com/lead-generation-for-charites/).

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/Karma-Campaigns-for-Nonprofits-e1688935734570.jpg?resize=800%2C282&ssl=1)](https://karmacampaigns.com/)

---

## Other conversion and e-commerce tracking

If there are other events that you want to track beyond the Enhanced measurement events GA4 tracks, you will need to [set them up with Google Tag Manager.](https://measureschool.com/how-to-track-events-with-ga4/#3)

#### Tracking donations with e-commerce tracking

If you collect donations through your website or have other e-commerce transactions (sales), you should set up e-commerce tracking to view transaction data in GA4 reports.

[Quality fundraising platforms](https://karmacampaigns.com/blog/best-online-donation-platforms/) and e-commerce tools will have GA4 e-commerce tracking built-in. Search your platform’s documentation to find out how to enable it.

#### Conclusion

With the shift from Universal Analytics to GA4, it is essential for nonprofits to adapt and take advantage of the new features and opportunities presented. Accurately measuring the performance of marketing efforts is crucial for nonprofits to evaluate and optimise their strategies effectively.

With valuable insights into their marketing efforts, nonprofits can improve online performance and make informed decisions to achieve their mission more effectively.

## More Conversion examples

#### 1) Conversion for clicks to an external site

The click event tracks outbound clicks. It has a parameter that contains the URL called link\_URL.

These can be used to create a Conversion for clicks to an external site. For example, if you wanted to track clicks to an external donation site:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/19-Conversion-external-sites-3.png?resize=800%2C605&ssl=1)](https://karmacampaigns.com/)

#### 2) Track clicks on a hyperlinked email address

The click event and link\_url parameter could also be used to track clicks on a hyperlinked email address, as they always begin with “mailto”:.

The Conversion would need to be set up as follows:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/20-Conversion-emailclick-2.png?resize=800%2C553&ssl=1)](https://karmacampaigns.com/)

#### 3) Track clicks on a hyperlinked phone number

Hyperlinked phone numbers always begin with “tel:”.

The Conversion settings would be:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/21-conversion-tel-2.png?resize=800%2C566&ssl=1)](https://karmacampaigns.com/)

#### 4) Track clicks for file downloads

The file\_download event also uses the link\_url parameter. It can be used to track downloads of a particular file, set up like this:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/22-Conversion-file-download-4.jpg?resize=800%2C614&ssl=1)](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/22-Conversion-file-download-4.jpg?ssl=1)

#### 5) Track email sign ups

The form\_submit event and the form\_id parameter could be used to make a conversion for the submission of an email submission form:

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/23-Conversion-Form-3.png?resize=800%2C573&ssl=1)](https://karmacampaigns.com/)

## **►** **About the Author**

[Karma Campaigns](https://karmacampaigns.com/) provides digital marketing services for not-for-profits, social enterprises and socially conscious businesses. Founded by [Jo Boyle](https://www.linkedin.com/in/joboyledigital/), Karma campaigns specializes in [Google Analytics and Google Ads](https://karmacampaigns.com/google-ad-grant-management/), [Facebook Ads](https://karmacampaigns.com/facebook-ads-agency/), and [lead generation](https://karmacampaigns.com/lead-generation-for-charites/).

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/Karma-Campaigns-for-Nonprofits-e1688935734570.jpg?resize=800%2C282&ssl=1)](https://karmacampaigns.com/)

[![](https://i0.wp.com/www.nptechforgood.com/wp-content/uploads/2023/07/Karma-Campaigns-for-Charities-2.jpg?resize=800%2C643&ssl=1)](https://karmacampaigns.com/)
